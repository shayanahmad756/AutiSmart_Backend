from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from .ai import generate_personalized_quiz
from .core import get_current_user, get_database, json_safe, object_id, require_roles


router = APIRouter(prefix="/api/assessments", tags=["assessments"], dependencies=[Depends(get_current_user)])
LEVELS = ["easy", "intermediate", "advanced", "sensory"]


async def _child_for_user(database: AsyncIOMotorDatabase, child_id: str, user: dict, allow_expert: bool = False):
    child = await database.children.find_one({"_id": object_id(child_id, "child id")})
    if not child:
        raise HTTPException(404, "Child not found")
    if user.get("role") == "admin":
        return child
    if allow_expert and user.get("role") == "expert":
        return child
    if child.get("caregiverId") != user.get("_id"):
        raise HTTPException(403, "Access denied")
    return child


async def _persist_child_quiz(database: AsyncIOMotorDatabase, child: dict, questions: list[dict], generated_by: str = "ai"):
    now = datetime.now(timezone.utc)
    query = {"childId": child["_id"], "level": "easy"}
    current = await database.childquizzes.find_one(query)
    doc = {
        **query,
        "title": f"{child.get('name', 'Child')}'s Personalized Assessment Quiz",
        "description": f"Adaptive quiz tailored to {child.get('name', 'the child')}'s profile and progress",
        "questions": questions,
        "generatedBy": generated_by,
        "iteration": (current or {}).get("iteration", 0) + 1,
        "updatedAt": now,
    }
    if current:
        await database.childquizzes.update_one({"_id": current["_id"]}, {"$set": doc})
        doc["_id"] = current["_id"]
    else:
        doc["createdAt"] = now
        result = await database.childquizzes.insert_one(doc)
        doc["_id"] = result.inserted_id
    return doc


async def _regenerate_quiz(database: AsyncIOMotorDatabase, child_id: ObjectId):
    child = await database.children.find_one({"_id": child_id})
    if not child:
        return
    history = await database.assessmentresults.find({"childId": child_id}).sort("createdAt", -1).limit(5).to_list(length=5)
    try:
        questions = await generate_personalized_quiz(child, history)
        await _persist_child_quiz(database, child, questions)
    except Exception as error:
        print(f"Personalized quiz regeneration failed: {error}")


@router.get("/")
async def get_assessments(database: AsyncIOMotorDatabase = Depends(get_database)):
    assessments = await database.assessments.find({"isActive": True}, {"createdBy": 0, "updatedBy": 0}).sort("level", 1).to_list(length=None)
    grouped = {level: None for level in LEVELS}
    for assessment in assessments:
        grouped[assessment["level"]] = {"id": str(assessment["_id"]), "level": assessment["level"], "title": assessment.get("title"), "description": assessment.get("description"), "questions": assessment.get("questions", []), "formDefinition": assessment.get("formDefinition", [])}
    return {"success": True, "data": grouped}


@router.get("/admin/all")
async def get_all_assessments(database: AsyncIOMotorDatabase = Depends(get_database), _user: dict = Depends(require_roles("admin"))):
    assessments = await database.assessments.find({}).sort("createdAt", -1).to_list(length=None)
    return {"success": True, "data": json_safe(assessments)}


@router.get("/child/{child_id}/quiz")
async def get_child_quiz(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _child_for_user(database, child_id, user)
    quiz = await database.childquizzes.find_one({"childId": child["_id"], "level": "easy"})
    if quiz:
        data = {"id": str(quiz["_id"]), "title": quiz.get("title"), "description": quiz.get("description"), "questions": quiz.get("questions", []), "isPersonalized": True, "iteration": quiz.get("iteration", 1)}
    else:
        data = {"title": "Personalized Assessment Quiz", "description": "Your personalized quiz is being prepared by AI.", "questions": [], "isPersonalized": False, "pending": True, "iteration": 0}
    return {"success": True, "data": data}


@router.post("/child/{child_id}/generate")
async def generate_child_quiz(child_id: str, user: dict = Depends(require_roles("admin")), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await database.children.find_one({"_id": object_id(child_id, "child id")})
    if not child:
        raise HTTPException(404, "Child not found")
    history = await database.assessmentresults.find({"childId": child["_id"]}).sort("createdAt", -1).limit(5).to_list(length=5)
    questions = await generate_personalized_quiz(child, history)
    await _persist_child_quiz(database, child, questions, str(user["_id"]))
    return {"success": True, "message": f"Quiz generated for {child['name']}", "data": {"childName": child["name"], "questions": len(questions)}}


@router.post("/results", status_code=201)
async def save_result(payload: dict, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child_id = payload.get("childId")
    total_score = payload.get("totalScore")
    total_questions = payload.get("totalQuestions")
    if not child_id or total_score is None or not total_questions:
        raise HTTPException(400, "childId, totalScore, and totalQuestions are required")
    child = await _child_for_user(database, child_id, user)
    percentage = round(float(total_score) / (int(total_questions) * 3) * 100)
    autism_level = "Beginner Level" if percentage <= 40 else "Intermediate Level" if percentage <= 60 else "Advanced Level"
    now = datetime.now(timezone.utc)
    result_doc = {
        "childId": child["_id"], "submittedBy": user["_id"],
        "assessmentLevel": payload.get("assessmentLevel") or "all",
        "totalScore": total_score, "totalQuestions": total_questions,
        "scorePercentage": percentage, "autismLevel": autism_level,
        "categoryScores": payload.get("categoryScores", {}), "answers": payload.get("answers", []),
        "createdAt": now, "updatedAt": now,
    }
    result = await database.assessmentresults.insert_one(result_doc)
    background_tasks.add_task(_regenerate_quiz, database, child["_id"])
    return {"success": True, "message": "Result saved. Personalized quiz is being updated in the background.", "data": {"resultId": str(result.inserted_id), "autismLevel": autism_level, "scorePercentage": percentage}}


@router.get("/child/{child_id}/results")
async def get_child_results(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _child_for_user(database, child_id, user, allow_expert=True)
    results = await database.assessmentresults.find({"childId": child["_id"]}).sort("createdAt", -1).to_list(length=None)
    if results:
        average = round(sum(item.get("scorePercentage", 0) for item in results) / len(results))
        stats = {"latest": json_safe(results[0]), "totalAttempts": len(results), "avgScore": average}
    else:
        stats = None
    return {"success": True, "data": {"results": json_safe(results), "stats": stats}}


@router.get("/child/{child_id}/quiz-results-detailed")
async def detailed_quiz_results(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _child_for_user(database, child_id, user, allow_expert=True)
    quizzes = await database.childquizzes.find({"childId": child["_id"]}).to_list(length=None)
    latest = await database.assessmentresults.find_one({"childId": child["_id"]}, sort=[("createdAt", -1)])
    answer_map = {answer.get("questionId"): answer for answer in (latest or {}).get("answers", [])}
    grouped: dict[str, list[dict]] = {}
    seen = set()
    for quiz in quizzes:
        for question in quiz.get("questions", []):
            question_id = question.get("id")
            if question_id in seen:
                continue
            seen.add(question_id)
            answer = answer_map.get(question_id)
            index = answer.get("optionIndex") if answer else None
            category = question.get("category", "Other")
            grouped.setdefault(category, []).append({"id": question_id, "category": category, "question": question.get("question"), "options": question.get("options", []), "scores": question.get("scores", []), "quizLevel": quiz.get("level"), "selectedOptionIndex": index, "selectedOptionText": question.get("options", [])[index] if isinstance(index, int) and 0 <= index < len(question.get("options", [])) else None, "score": answer.get("score") if answer else None})
    result_summary = None
    if latest:
        result_summary = {key: json_safe(latest.get(key)) for key in ("assessmentLevel", "totalScore", "totalQuestions", "scorePercentage", "autismLevel", "categoryScores", "createdAt")}
    return {"success": True, "data": {"child": {"_id": str(child["_id"]), "name": child.get("name"), "age": child.get("age")}, "assessmentResult": result_summary, "questionsByCategory": grouped, "hasData": bool(grouped)}}


@router.get("/{level}")
async def get_assessment_by_level(level: str, database: AsyncIOMotorDatabase = Depends(get_database)):
    if level not in LEVELS:
        raise HTTPException(400, "Invalid level. Must be one of: easy, intermediate, advanced, sensory")
    assessment = await database.assessments.find_one({"level": level, "isActive": True}, {"createdBy": 0, "updatedBy": 0})
    if not assessment:
        raise HTTPException(404, f'No active assessment found for level "{level}"')
    return {"success": True, "data": {"id": str(assessment["_id"]), "level": assessment["level"], "title": assessment.get("title"), "description": assessment.get("description"), "questions": assessment.get("questions", []), "formDefinition": assessment.get("formDefinition", [])}}