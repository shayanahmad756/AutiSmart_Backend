import argparse
import asyncio
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from motor.motor_asyncio import AsyncIOMotorClient

from app.core import settings


def question(identifier, category, text, options):
    return {"id": identifier, "category": category, "question": text, "options": options, "scores": [1, 2, 3]}


ASSESSMENTS = [
    {
        "level": "easy", "title": "Level 1 - Basic Autism Assessment", "description": "Initial screening for basic autism characteristics", "isActive": True, "formDefinition": [],
        "questions": [
            question("easy_q1", "Eye Contact", "How often does the child make eye contact during conversations?", ["Frequently maintains eye contact", "Sometimes makes eye contact", "Rarely or never makes eye contact"]),
            question("easy_q2", "Social Interaction", "How does the child respond to social situations with peers?", ["Actively engages with peers", "Shows some interest but hesitant", "Avoids or shows no interest"]),
            question("easy_q3", "Communication", "How does the child express their needs and wants?", ["Uses clear verbal communication", "Uses gestures or limited words", "Has difficulty expressing needs"]),
            question("easy_q4", "Repetitive Behavior", "Does the child engage in repetitive movements or actions?", ["No repetitive behaviors", "Occasional repetitive actions", "Frequent repetitive behaviors"]),
            question("easy_q5", "Sensory Sensitivity", "How does the child react to sensory stimuli (sounds, lights, textures)?", ["Typical reactions", "Sometimes oversensitive", "Highly sensitive or avoidant"]),
        ],
    },
    {
        "level": "intermediate", "title": "Level 2 - Intermediate Autism Assessment", "description": "Detailed assessment for identifying moderate autism characteristics", "isActive": True, "formDefinition": [],
        "questions": [
            question("int_q1", "Social Interaction", "How does the child handle changes in routine or unexpected events?", ["Adapts easily to changes", "Shows some distress but recovers", "Becomes very upset or resistant"]),
            question("int_q2", "Communication", "Can the child understand and use non-verbal communication (gestures, facial expressions)?", ["Understands and uses effectively", "Limited understanding or use", "Rarely understands or uses"]),
            question("int_q3", "Focus & Attention", "How long can the child maintain attention on a task?", ["Age-appropriate attention span", "Shorter than typical attention span", "Very brief or easily distracted"]),
            question("int_q4", "Repetitive Behavior", "Does the child have intense, focused interests in specific topics?", ["Varied interests", "Strong preference for certain topics", "Extremely focused on one topic"]),
            question("int_q5", "Social Interaction", "How does the child engage in imaginative or pretend play?", ["Engages in varied pretend play", "Limited or repetitive pretend play", "Does not engage in pretend play"]),
            question("int_q6", "Communication", "Does the child understand jokes, sarcasm, or figurative language?", ["Usually understands", "Sometimes understands with help", "Takes language literally"]),
        ],
    },
    {
        "level": "advanced", "title": "Level 3 - Advanced Autism Assessment", "description": "Comprehensive assessment for identifying complex autism characteristics", "isActive": True, "formDefinition": [],
        "questions": [
            question("adv_q1", "Social Interaction", "How does the child recognize and respond to others' emotions?", ["Recognizes and responds appropriately", "Sometimes recognizes but unsure how to respond", "Difficulty recognizing emotions"]),
            question("adv_q2", "Communication", "Can the child maintain a back-and-forth conversation?", ["Yes, maintains conversations well", "Sometimes, but may go off-topic", "Rarely maintains conversation flow"]),
            question("adv_q3", "Social Interaction", "Does the child seek to share interests or achievements with others?", ["Frequently shares with others", "Occasionally shares", "Rarely or never shares"]),
            question("adv_q4", "Repetitive Behavior", "Does the child insist on sameness or become distressed by small changes?", ["Flexible with changes", "Prefers routine but adapts", "Very rigid, distressed by changes"]),
            question("adv_q5", "Communication", "How does the child use and understand body language in social situations?", ["Uses appropriately", "Limited or awkward use", "Does not use or understand"]),
            question("adv_q6", "Focus & Attention", "Can the child shift attention between activities smoothly?", ["Shifts attention easily", "Sometimes has difficulty", "Very difficult, gets stuck on tasks"]),
            question("adv_q7", "Sensory Sensitivity", "Does the child show unusual responses to pain, temperature, or sensory input?", ["Typical responses", "Sometimes unusual responses", "Frequently unusual responses"]),
        ],
    },
    {
        "level": "sensory", "title": "Bonus - Sensory & Attention Assessment", "description": "Specialized assessment focusing on sensory processing and attention patterns", "isActive": True, "formDefinition": [],
        "questions": [
            question("sen_q1", "Sensory Sensitivity", "How does the child react to loud or unexpected noises?", ["Normal reaction", "Startles easily or covers ears", "Extreme distress or meltdown"]),
            question("sen_q2", "Sensory Sensitivity", "Does the child have strong preferences or aversions to certain textures (food, clothing)?", ["Few or no texture issues", "Some texture preferences", "Very strong aversions"]),
            question("sen_q3", "Focus & Attention", "Can the child filter out background noise to focus on important sounds?", ["Filters effectively", "Sometimes distracted", "Very distracted by background sounds"]),
            question("sen_q4", "Sensory Sensitivity", "How does the child respond to bright lights or visual stimuli?", ["Normal response", "Squints or avoids bright lights", "Strong aversion or distress"]),
            question("sen_q5", "Focus & Attention", "Does the child have difficulty transitioning between activities?", ["Transitions smoothly", "Needs reminders or time", "Very difficult, resists transitions"]),
            question("sen_q6", "Sensory Sensitivity", "Does the child seek or avoid physical touch and close contact?", ["Comfortable with touch", "Selective about touch", "Strongly avoids or seeks touch"]),
        ],
    },
]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Replace all existing assessments")
    args = parser.parse_args()
    if not settings.mongo_uri:
        raise SystemExit("Set MONGO_URI in backend/.env first")
    client = AsyncIOMotorClient(settings.mongo_uri)
    try:
        database_name = unquote(urlsplit(settings.mongo_uri).path.lstrip("/").split("/", 1)[0]) or settings.mongo_db
        database = client[database_name]
        admin = await database.users.find_one({"role": "admin"})
        if not admin:
            raise SystemExit("Create an admin account first with: py -m scripts.create_admin")
        if await database.assessments.count_documents({}) and not args.force:
            print("Assessments already exist. Skipped; pass --force to replace them.")
            return
        if args.force:
            await database.assessments.delete_many({})
        now = datetime.now(timezone.utc)
        records = [{**assessment, "createdBy": admin["_id"], "updatedBy": admin["_id"], "createdAt": now, "updatedAt": now} for assessment in ASSESSMENTS]
        result = await database.assessments.insert_many(records)
        print(f"Created {len(result.inserted_ids)} assessments")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())