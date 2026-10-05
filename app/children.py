import base64
import io
import os
import re
from datetime import datetime, timezone

import httpx
from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .ai import emotion_feedback, generate_personalized_quiz, _json_completion
from .assessments import _persist_child_quiz
from .core import get_current_user, get_database, json_safe, object_id, settings as core_settings


router = APIRouter(prefix="/api/caregiver", tags=["children"], dependencies=[Depends(get_current_user)])
CHAT_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "chat")
os.makedirs(CHAT_UPLOAD_DIR, exist_ok=True)
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
CHAT_TYPES = IMAGE_TYPES | {"application/pdf", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain"}


async def _authorized_child(database: AsyncIOMotorDatabase, child_id: str, user: dict) -> dict:
    child = await database.children.find_one({"_id": object_id(child_id, "child id")})
    if not child:
        raise HTTPException(404, "Child not found")
    role = user.get("role")
    if role == "admin" or child.get("caregiverId") == user.get("_id"):
        return child
    if role == "expert":
        owner = await database.users.find_one({"_id": child.get("caregiverId"), "expertRequests": {"$elemMatch": {"expertId": user["_id"], "status": "approved"}}}, {"_id": 1})
        if owner:
            return child
    raise HTTPException(403, "Access denied")


async def _child_stats(database: AsyncIOMotorDatabase, child_id: ObjectId) -> dict:
    activities = await database.activities.find({"childId": child_id}).sort("completedAt", -1).limit(1000).to_list(length=1000)
    stats = {"totalActivities": len(activities), "averageScore": 0, "totalGames": 0, "totalAssessments": 0, "totalTherapySessions": 0, "byActivityType": {}, "progressOverTime": [], "recentActivities": json_safe(activities[:10]), "gamesList": [], "assessmentsList": []}
    if not activities:
        return stats
    score_sum = 0
    daily = {}
    for activity in activities:
        percentage = activity.get("percentage", 0) or 0
        score_sum += percentage
        kind = activity.get("activityType")
        name = activity.get("activityName", "Activity")
        if kind == "game":
            stats["totalGames"] += 1
            stats["gamesList"].append({"name": name, "score": percentage, "completedAt": json_safe(activity.get("completedAt")), "timeTaken": activity.get("duration", 0)})
        elif kind == "assessment":
            stats["totalAssessments"] += 1
            stats["assessmentsList"].append({"name": name, "score": percentage, "completedAt": json_safe(activity.get("completedAt")), "category": activity.get("category", "General")})
        elif kind == "therapy":
            stats["totalTherapySessions"] += 1
        grouped = stats["byActivityType"].setdefault(name, {"count": 0, "totalScore": 0, "averageScore": 0})
        grouped["count"] += 1
        grouped["totalScore"] += percentage
        completed = activity.get("completedAt") or activity.get("createdAt") or datetime.now(timezone.utc)
        day = completed.date().isoformat() if isinstance(completed, datetime) else str(completed)[:10]
        bucket = daily.setdefault(day, {"date": day, "count": 0, "totalScore": 0})
        bucket["count"] += 1
        bucket["totalScore"] += percentage
    stats["averageScore"] = score_sum / len(activities)
    for grouped in stats["byActivityType"].values():
        grouped["averageScore"] = grouped["totalScore"] / grouped["count"]
    stats["progressOverTime"] = [{**item, "averageScore": item["totalScore"] / item["count"]} for _, item in sorted(daily.items())]
    return stats


async def _regenerate_quiz(database: AsyncIOMotorDatabase, child_id: ObjectId):
    child = await database.children.find_one({"_id": child_id})
    if not child:
        return
    history = await database.assessmentresults.find({"childId": child_id}).sort("createdAt", -1).limit(5).to_list(length=5)
    try:
        questions = await generate_personalized_quiz(child, history)
        await _persist_child_quiz(database, child, questions)
    except Exception as error:
        print(f"Quiz generation skipped: {error}")


@router.get("/all-children")
async def get_all_children(user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    if user.get("role") == "admin":
        children = await database.children.find({}).sort("createdAt", -1).to_list(length=None)
    elif user.get("role") == "expert":
        caregivers = await database.users.find({"role": "caregiver", "expertRequests": {"$elemMatch": {"expertId": user["_id"], "status": "approved"}}}, {"_id": 1}).to_list(length=None)
        caregiver_ids = [caregiver["_id"] for caregiver in caregivers]
        children = await database.children.find({"caregiverId": {"$in": caregiver_ids}}).sort("createdAt", -1).to_list(length=None)
        for child in children:
            activities = await database.activities.find({"childId": child["_id"]}).sort("completedAt", -1).to_list(length=1)
            child["latestActivityAt"] = activities[0].get("completedAt") if activities else None
            child["latestScore"] = activities[0].get("percentage") if activities else None
            child["totalActivities"] = await database.activities.count_documents({"childId": child["_id"]})
    else:
        raise HTTPException(403, "Only experts and admins can access all children")
    return {"success": True, "data": json_safe(children), "count": len(children)}


@router.post("/children", status_code=201)
async def add_child(payload: dict, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    if user.get("role") != "caregiver":
        raise HTTPException(403, "Only caregivers can add children")
    name = str(payload.get("name", "")).strip()
    try:
        age = int(payload.get("age"))
    except (TypeError, ValueError) as error:
        raise HTTPException(400, "A valid age is required") from error
    gender = payload.get("gender")
    if not name or not 1 <= age <= 18 or gender not in {"Male", "Female", "Other"}:
        raise HTTPException(400, "Name, age (1-18), and a valid gender are required")
    now = datetime.now(timezone.utc)
    child = {"caregiverId": user["_id"], "name": name, "age": age, "gender": gender, "dateOfBirth": payload.get("dateOfBirth") or "", "diagnosis": payload.get("diagnosis", ""), "specialNeeds": payload.get("specialNeeds", ""), "notes": payload.get("notes", ""), "profileImage": payload.get("profileImage", ""), "autismDetections": [], "createdAt": now, "updatedAt": now}
    result = await database.children.insert_one(child)
    child["_id"] = result.inserted_id
    background_tasks.add_task(_regenerate_quiz, database, result.inserted_id)
    return {"success": True, "message": "Child added successfully", "data": json_safe(child)}


@router.get("/children")
async def get_children(user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    if user.get("role") != "caregiver":
        raise HTTPException(403, "Only caregivers can access their children")
    children = await database.children.find({"caregiverId": user["_id"]}).sort("createdAt", -1).to_list(length=None)
    return {"success": True, "data": json_safe(children), "count": len(children)}


@router.get("/children/{child_id}")
async def get_child(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    return {"success": True, "data": json_safe(child)}


@router.put("/children/{child_id}")
async def update_child(child_id: str, payload: dict, background_tasks: BackgroundTasks, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    if user.get("role") != "caregiver":
        raise HTTPException(403, "Only caregivers can update children")
    allowed = {"name", "age", "gender", "dateOfBirth", "diagnosis", "specialNeeds", "notes", "profileImage"}
    updates = {key: value for key, value in payload.items() if key in allowed}
    if "age" in updates:
        updates["age"] = int(updates["age"])
        if not 1 <= updates["age"] <= 18:
            raise HTTPException(400, "Age must be between 1 and 18")
    updates["updatedAt"] = datetime.now(timezone.utc)
    result = await database.children.find_one_and_update({"_id": child["_id"]}, {"$set": updates}, return_document=True)
    background_tasks.add_task(_regenerate_quiz, database, child["_id"])
    return {"success": True, "message": "Child updated successfully", "data": json_safe(result)}


@router.delete("/children/{child_id}")
async def delete_child(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    if user.get("role") != "caregiver":
        raise HTTPException(403, "Only caregivers can delete children")
    await database.activities.delete_many({"childId": child["_id"]})
    await database.assessmentresults.delete_many({"childId": child["_id"]})
    await database.childquizzes.delete_many({"childId": child["_id"]})
    await database.children.delete_one({"_id": child["_id"]})
    return {"success": True, "message": "Child and associated data deleted successfully"}


@router.get("/children/{child_id}/stats")
async def get_child_stats(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    stats = await _child_stats(database, child["_id"])
    return {"success": True, "data": {"child": json_safe(child), **stats}}


@router.get("/children/{child_id}/activities")
async def get_child_activities(child_id: str, type: str | None = None, limit: int | None = Query(default=None, ge=1, le=1000), user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    query = {"childId": child["_id"]}
    if type:
        query["activityType"] = type
    cursor = database.activities.find(query).sort("completedAt", -1)
    activities = await cursor.to_list(length=limit)
    return {"success": True, "data": json_safe(activities), "count": len(activities)}


@router.post("/children/{child_id}/activities", status_code=201)
async def add_activity(child_id: str, payload: dict, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    if payload.get("activityType") not in {"game", "assessment", "therapy"} or not payload.get("activityName"):
        raise HTTPException(400, "activityType and activityName are required")
    now = datetime.now(timezone.utc)
    activity = {**payload, "childId": child["_id"], "caregiverId": child.get("caregiverId", user["_id"]), "score": payload.get("score", 0), "maxScore": payload.get("maxScore", 0), "percentage": payload.get("percentage", 0), "duration": payload.get("duration", 0), "attempts": payload.get("attempts", 1), "details": payload.get("details", {}), "completedAt": now, "createdAt": now, "updatedAt": now}
    result = await database.activities.insert_one(activity)
    activity["_id"] = result.inserted_id
    return {"success": True, "message": "Activity recorded successfully", "data": json_safe(activity)}


@router.get("/children/{child_id}/report")
async def child_report(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    stats = await _child_stats(database, child["_id"])
    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, title=f"{child.get('name', 'Child')} Progress Report")
    styles = getSampleStyleSheet()
    content = [Paragraph("CHILD PROGRESS REPORT", styles["Title"]), Paragraph(f"Generated {datetime.now().strftime('%B %d, %Y')}", styles["Normal"]), Spacer(1, 18)]
    details = [["Child", child.get("name", "")], ["Age", str(child.get("age", ""))], ["Gender", child.get("gender", "")], ["Diagnosis", child.get("diagnosis") or "Not specified"], ["Special needs", child.get("specialNeeds") or "Not specified"]]
    details.extend([["Total activities", str(stats["totalActivities"])], ["Games", str(stats["totalGames"])], ["Assessments", str(stats["totalAssessments"])], ["Therapy sessions", str(stats["totalTherapySessions"])], ["Average score", f"{stats['averageScore']:.1f}%"]])
    table = Table(details, colWidths=[150, 350])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#e7f4f1")), ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("PADDING", (0, 0), (-1, -1), 8)]))
    content.append(table)
    content.extend([Spacer(1, 18), Paragraph("Recent activities", styles["Heading2"])])
    for activity in stats["recentActivities"]:
        content.append(Paragraph(f"{activity.get('activityName', 'Activity')} ({activity.get('activityType', '')}): {activity.get('percentage', 0)}%", styles["BodyText"]))
    document.build(content)
    filename = f"child-report-{re.sub(r'\s+', '-', child.get('name', 'child'))}-{int(datetime.now().timestamp())}.pdf"
    return {"success": True, "data": base64.b64encode(buffer.getvalue()).decode("ascii"), "filename": filename}


@router.post("/children/{child_id}/emotion-scenarios")
async def get_emotion_scenarios(child_id: str, payload: dict, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    if user.get("role") not in {"caregiver", "admin"}:
        raise HTTPException(403, "Access denied")
    pool = payload.get("pool") if isinstance(payload.get("pool"), list) else []
    count = max(1, min(int(payload.get("count", 10)), 20))
    prompt = f"""Create {count} short scenarios for an emotion recognition game for a child aged {child.get('age')}. Level: {payload.get('level', 7)} ({payload.get('levelName', 'Advanced')}). Child profile notes: {child.get('specialNeeds', '')} {child.get('diagnosis', '')}. Use ONLY these emotion labels: {', '.join(pool)}. Each scenario should have a 1-2 sentence everyday situation and exactly 3 distinct distractor emotions from the provided list, excluding the correct emotion. Return JSON with a scenarios array of {{emotion, scenario, distractors}}."""
    data = await _json_completion(prompt)
    allowed = set(pool)
    scenarios = []
    for item in data.get("scenarios", []):
        emotion = item.get("emotion")
        distractors = [value for value in item.get("distractors", []) if value in allowed and value != emotion]
        if emotion in allowed and item.get("scenario") and len(distractors) >= 3:
            scenarios.append({"emotion": emotion, "scenario": item["scenario"].strip(), "distractors": distractors[:3]})
        if len(scenarios) >= count:
            break
    return {"success": True, "scenarios": scenarios}


@router.post("/children/{child_id}/emotion-feedback")
async def get_emotion_feedback(child_id: str, payload: dict, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    result = await emotion_feedback(child, payload)
    return {"success": True, "feedback": result}


@router.get("/children/{child_id}/recommendations")
async def get_recommendations(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    results = await database.assessmentresults.find({"childId": child["_id"]}).sort("createdAt", -1).to_list(length=1)
    games = [
        (1, "Memory Match", ["Focus & Attention", "Repetitive Behavior"], "/games/memory-match"),
        (2, "Sound Matching", ["Sensory Sensitivity", "Communication"], "/games/sound-matching"),
        (10, "Color Matching", ["Focus & Attention", "Sensory Sensitivity"], "/games/color-matching"),
        (3, "Emotion Explorer", ["Eye Contact", "Social Interaction"], "/games/emotion-explorer"),
        (13, "Communication Builder", ["Communication", "Social Interaction"], "/games/picture-word"),
        (12, "Eye Contact Game", ["Eye Contact", "Social Interaction"], "/games/eye-contact"),
        (9, "Pattern Builder", ["Repetitive Behavior", "Focus & Attention"], "/games/pattern-builder"),
    ]
    if not results:
        return {"success": True, "data": {"hasAssessment": False, "recommendations": []}}
    latest = results[0]
    severities = {category: (data.get("score", 0) / (data.get("total", 1) * 3)) if data.get("total", 0) > 0 else 0 for category, data in latest.get("categoryScores", {}).items()}
    recommendations = []
    for game_id, name, categories, route in games:
        areas = [category for category in categories if severities.get(category, 0) > 0.4]
        score = round(sum(severities.get(category, 0) for category in areas), 4)
        recommendations.append({"id": game_id, "name": name, "route": route, "relevanceScore": score, "isRecommended": score > 0, "problemAreas": areas, "targetedCategories": categories})
    recommendations.sort(key=lambda item: item["relevanceScore"], reverse=True)
    return {"success": True, "data": {"hasAssessment": True, "assessmentDate": json_safe(latest.get("createdAt")), "autismLevel": latest.get("autismLevel"), "categorySeverities": severities, "recommendations": recommendations}}


@router.post("/children/{child_id}/autism-detect")
async def autism_detect(child_id: str, image: UploadFile | None = File(default=None), note: str = "", user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    if not image:
        raise HTTPException(400, 'No image file provided. Include an image with field name "image".')
    if image.content_type not in IMAGE_TYPES:
        raise HTTPException(400, "Unsupported image type. Upload JPEG, PNG, GIF, WebP, or BMP.")
    data = await image.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Image exceeds the 10 MB upload limit")
    child = await _authorized_child(database, child_id, user)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{core_settings.ml_url.rstrip('/')}/predict", files={"file": (image.filename or "upload.jpg", data, image.content_type)})
        response.raise_for_status()
        prediction = response.json()
    except httpx.ConnectError as error:
        raise HTTPException(503, "Autism detection service is not running. Start the ML service on port 5001.") from error
    except (httpx.HTTPError, ValueError) as error:
        raise HTTPException(502, f"Detection service error: {error}") from error
    detection = {"date": datetime.now(timezone.utc), "label": prediction.get("label"), "confidence": prediction.get("confidence"), "note": note}
    await database.children.update_one({"_id": child["_id"]}, {"$push": {"autismDetections": {"$each": [detection], "$position": 0}}})
    return {"success": True, "message": "Detection completed and saved.", "data": {**prediction, **json_safe(detection), "childName": child.get("name")}}


@router.get("/children/{child_id}/autism-detections")
async def autism_detections(child_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    child = await _authorized_child(database, child_id, user)
    detections = child.get("autismDetections", [])
    return {"success": True, "data": {"childName": child.get("name"), "detections": json_safe(detections), "count": len(detections)}}