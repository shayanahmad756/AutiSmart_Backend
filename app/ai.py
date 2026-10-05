import json

from fastapi import HTTPException
from groq import AsyncGroq

from .core import settings


VALID_CATEGORIES = [
    "Eye Contact",
    "Social Interaction",
    "Communication",
    "Repetitive Behavior",
    "Sensory Sensitivity",
    "Focus & Attention",
]
GROQ_MODEL = "llama-3.3-70b-versatile"


def _client() -> AsyncGroq:
    if not settings.groq_api_key:
        raise HTTPException(503, "Groq API key not configured. Add GROQ_API_KEY to backend/.env")
    return AsyncGroq(api_key=settings.groq_api_key)


async def _json_completion(prompt: str) -> dict:
    completion = await _client().chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.7,
    )
    text = completion.choices[0].message.content or "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise HTTPException(502, "AI provider returned invalid JSON") from error


async def generate_quiz_questions(level: str, categories: list[str] | None = None, count: int = 5) -> list[dict]:
    categories = [category for category in (categories or VALID_CATEGORIES) if category in VALID_CATEGORIES]
    if not categories:
        categories = VALID_CATEGORIES
    level_descriptions = {
        "easy": "basic observation-based questions suitable for initial screening",
        "intermediate": "situational and behavior-awareness questions at a moderate difficulty",
        "advanced": "complex reasoning and social-thinking questions",
        "sensory": "specialized sensory processing and attention questions",
    }
    prompt = f"""You are a child-development assessment writer. Generate {count} observable caregiver-report multiple-choice questions for a {level} level assessment ({level_descriptions.get(level, level)}).
Focus on: {', '.join(categories)}. Questions must be clear, non-clinical, and age respectful. Each question must have exactly 3 options and scores [1, 2, 3], where 1 is a positive/typical response and 3 indicates more support may be useful. Use only these categories: {', '.join(VALID_CATEGORIES)}.
Return JSON only as {{"questions":[{{"id":"{level}_gen_1","category":"Social Interaction","question":"...","options":["...","...","..."],"scores":[1,2,3]}}]}}."""
    result = await _json_completion(prompt)
    questions = result.get("questions", [])
    sanitized = []
    for index, question in enumerate(questions[:count]):
        options = question.get("options")
        scores = question.get("scores")
        sanitized.append({
            "id": str(question.get("id") or f"{level}_gen_{index + 1}"),
            "category": question.get("category") if question.get("category") in VALID_CATEGORIES else categories[index % len(categories)],
            "question": str(question.get("question", "")),
            "options": options if isinstance(options, list) and len(options) == 3 else ["Often", "Sometimes", "Not yet"],
            "scores": [int(score) for score in scores] if isinstance(scores, list) and len(scores) == 3 else [1, 2, 3],
        })
    if not sanitized:
        raise HTTPException(502, "AI provider did not return assessment questions")
    return sanitized


async def generate_personalized_quiz(child: dict, past_results: list[dict]) -> list[dict]:
    history = [{"level": item.get("assessmentLevel"), "categoryScores": item.get("categoryScores", {})} for item in past_results[:5]]
    prompt = f"""You are an expert child-development assessment writer. Create 10 personalized caregiver-report questions for this child profile: {json.dumps({key: child.get(key) for key in ('name', 'age', 'gender', 'diagnosis', 'specialNeeds', 'notes')}, default=str)}.
Past assessments: {json.dumps(history, default=str)}.
Use these categories only: {', '.join(VALID_CATEGORIES)}. Each question must have exactly 3 clear, observable options and scores [1, 2, 3] (positive/typical to more support needed). Avoid diagnosis or medical conclusions.
Return JSON only with a questions array; each question has id, category, question, options, scores."""
    return await generate_quiz_questions_from_prompt(prompt, "personalized", 10)


async def generate_quiz_questions_from_prompt(prompt: str, key: str, count: int) -> list[dict]:
    result = await _json_completion(prompt)
    questions = result.get("questions", [])
    output = []
    for index, question in enumerate(questions[:count]):
        options = question.get("options")
        scores = question.get("scores")
        output.append({
            "id": str(question.get("id") or f"{key}_{index + 1}"),
            "category": question.get("category") if question.get("category") in VALID_CATEGORIES else VALID_CATEGORIES[index % len(VALID_CATEGORIES)],
            "question": str(question.get("question", "")),
            "options": options if isinstance(options, list) and len(options) == 3 else ["Often", "Sometimes", "Not yet"],
            "scores": [int(score) for score in scores] if isinstance(scores, list) and len(scores) == 3 else [1, 2, 3],
        })
    if not output:
        raise HTTPException(502, "AI provider did not return questions")
    return output


async def chat_reply(message: str, history: list[dict] | None = None) -> str:
    client = _client()
    messages = [{"role": "system", "content": "You are AutiSmart AI, a compassionate assistant for caregivers and experts supporting autistic children. Offer concise, evidence-informed, non-diagnostic guidance. Encourage consulting qualified professionals for clinical decisions."}]
    for turn in (history or [])[-10:]:
        role = "assistant" if turn.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": str(turn.get("content", ""))})
    messages.append({"role": "user", "content": message})
    completion = await client.chat.completions.create(model=GROQ_MODEL, messages=messages, max_tokens=600, temperature=0.7)
    return completion.choices[0].message.content or "I couldn't generate a response. Please try again."


async def emotion_scenarios(payload: dict) -> list[dict]:
    prompt = f"""Create 5 short, age-appropriate emotional scenarios for a child aged {payload.get('age', 'unknown')}. Focus on {payload.get('emotion', 'understanding emotions')}. Each scenario should ask what someone might feel and why, without a single rigid correct answer. Return JSON {{"scenarios":[{{"situation":"...","options":["...","...","..."],"correctAnswer":0,"explanation":"..."}}]}}."""
    data = await _json_completion(prompt)
    return data.get("scenarios", [])


async def emotion_feedback(child: dict, payload: dict) -> str:
    score = payload.get("score", 0)
    maximum = payload.get("maxScore", 100)
    percent = round(score / maximum * 100) if maximum else 0
    emotions = ", ".join(payload.get("emotionsStruggled", [])) or "none reported"
    prompt = f"Write 2-3 short, warm, age-appropriate feedback sentences for {child.get('name', 'the child')}, age {child.get('age', 'unknown')}, after Emotion Explorer level {payload.get('level', 1)} ({payload.get('levelName', 'Beginner')}). Score: {score}/{maximum} ({percent}%). Correct: {payload.get('correctAnswers', 0)}; incorrect: {payload.get('incorrectAnswers', 0)}. Emotions they found difficult: {emotions}. Celebrate effort, encourage gently, do not diagnose or label the child. Return plain text only."
    completion = await _client().chat.completions.create(model=GROQ_MODEL, messages=[{"role": "user", "content": prompt}], max_tokens=180, temperature=0.6)
    return completion.choices[0].message.content or "Thanks for sharing your thinking."