from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from .core import get_current_user, get_database, hash_password, json_safe, object_id, require_roles


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_roles("admin"))])


@router.get("/stats")
async def get_stats(database: AsyncIOMotorDatabase = Depends(get_database)):
    users = database.users
    total = await users.count_documents({})
    verified = await users.count_documents({"isVerified": True})
    return {"success": True, "data": {"totalUsers": total, "caregivers": await users.count_documents({"role": "caregiver"}), "experts": await users.count_documents({"role": "expert"}), "admins": await users.count_documents({"role": "admin"}), "verifiedUsers": verified, "unverifiedUsers": total - verified}}


@router.post("/users", status_code=status.HTTP_201_CREATED)
async def create_user(payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    name = str(payload.get("name", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    role = payload.get("role", "caregiver")
    if not name or not email or len(password) < 6:
        raise HTTPException(400, "Name, email, and a password of at least 6 characters are required")
    if role not in {"caregiver", "expert", "admin"}:
        raise HTTPException(400, "Invalid role")
    if await database.users.find_one({"email": email}):
        raise HTTPException(409, "Email already registered")
    now = datetime.now(timezone.utc)
    user = {"name": name, "email": email, "password": hash_password(password), "role": role, "isVerified": payload.get("isVerified", True), "expertRequests": [], "createdAt": now, "updatedAt": now}
    result = await database.users.insert_one(user)
    user["_id"] = result.inserted_id
    return {"success": True, "message": "User created successfully", "data": json_safe(user)}


@router.get("/users")
async def get_users(role: str | None = None, verified: bool | None = None, database: AsyncIOMotorDatabase = Depends(get_database)):
    filters = {}
    if role:
        filters["role"] = role
    if verified is not None:
        filters["isVerified"] = verified
    users = await database.users.find(filters, {"password": 0, "otp": 0}).sort("createdAt", -1).to_list(length=None)
    return {"success": True, "count": len(users), "data": json_safe(users)}


@router.get("/users/{user_id}")
async def get_user(user_id: str, database: AsyncIOMotorDatabase = Depends(get_database)):
    user = await database.users.find_one({"_id": object_id(user_id, "user id")}, {"password": 0, "otp": 0})
    if not user:
        raise HTTPException(404, "User not found")
    return {"success": True, "data": json_safe(user)}


@router.put("/users/{user_id}")
async def update_user(user_id: str, payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    identifier = object_id(user_id, "user id")
    changes = {key: value for key, value in payload.items() if key in {"name", "email", "role", "isVerified", "phone", "phoneNumber"}}
    if "email" in changes:
        changes["email"] = str(changes["email"]).strip().lower()
    if "role" in changes and changes["role"] not in {"caregiver", "expert", "admin"}:
        raise HTTPException(400, "Invalid role")
    changes["updatedAt"] = datetime.now(timezone.utc)
    result = await database.users.find_one_and_update({"_id": identifier}, {"$set": changes}, return_document=True)
    if not result:
        raise HTTPException(404, "User not found")
    return {"success": True, "message": "User updated successfully", "data": json_safe(result)}


@router.put("/users/{user_id}/role")
async def update_user_role(user_id: str, payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    role = payload.get("role")
    if role not in {"caregiver", "expert", "admin"}:
        raise HTTPException(400, "Invalid role")
    result = await database.users.find_one_and_update({"_id": object_id(user_id, "user id")}, {"$set": {"role": role, "updatedAt": datetime.now(timezone.utc)}}, return_document=True)
    if not result:
        raise HTTPException(404, "User not found")
    return {"success": True, "message": "User role updated successfully", "data": json_safe(result)}


@router.put("/users/{user_id}/toggle-verification")
async def toggle_verification(user_id: str, database: AsyncIOMotorDatabase = Depends(get_database)):
    identifier = object_id(user_id, "user id")
    user = await database.users.find_one({"_id": identifier})
    if not user:
        raise HTTPException(404, "User not found")
    result = await database.users.find_one_and_update({"_id": identifier}, {"$set": {"isVerified": not user.get("isVerified", False)}}, return_document=True)
    return {"success": True, "message": "User verification status updated", "data": json_safe(result)}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, database: AsyncIOMotorDatabase = Depends(get_database)):
    result = await database.users.delete_one({"_id": object_id(user_id, "user id")})
    if result.deleted_count == 0:
        raise HTTPException(404, "User not found")
    return {"success": True, "message": "User deleted successfully"}


@router.get("/assessments")
async def get_assessments(database: AsyncIOMotorDatabase = Depends(get_database)):
    assessments = await database.assessments.find({}).sort("createdAt", -1).to_list(length=None)
    return {"success": True, "count": len(assessments), "data": json_safe(assessments)}


@router.post("/assessments", status_code=status.HTTP_201_CREATED)
async def create_assessment(payload: dict, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    level = payload.get("level")
    if level not in {"easy", "intermediate", "advanced", "sensory"} or not payload.get("title") or not payload.get("description"):
        raise HTTPException(400, "Level, title, and description are required")
    active = payload.get("isActive", True)
    if active and await database.assessments.find_one({"level": level, "isActive": True}):
        raise HTTPException(409, f"An active assessment already exists for level '{level}'")
    now = datetime.now(timezone.utc)
    assessment = {"level": level, "title": payload["title"], "description": payload["description"], "questions": payload.get("questions", []), "formDefinition": payload.get("formDefinition", []), "isActive": active, "createdBy": user["_id"], "createdAt": now, "updatedAt": now}
    result = await database.assessments.insert_one(assessment)
    assessment["_id"] = result.inserted_id
    return {"success": True, "message": "Assessment created successfully", "data": json_safe(assessment)}


@router.put("/assessments/{assessment_id}")
async def update_assessment(assessment_id: str, payload: dict, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    identifier = object_id(assessment_id, "assessment id")
    current = await database.assessments.find_one({"_id": identifier})
    if not current:
        raise HTTPException(404, "Assessment not found")
    changes = {key: value for key, value in payload.items() if key in {"level", "title", "description", "questions", "formDefinition", "isActive"}}
    level = changes.get("level", current.get("level"))
    active = changes.get("isActive", current.get("isActive"))
    if level not in {"easy", "intermediate", "advanced", "sensory"}:
        raise HTTPException(400, "Invalid assessment level")
    if active and await database.assessments.find_one({"_id": {"$ne": identifier}, "level": level, "isActive": True}):
        raise HTTPException(409, f"An active assessment already exists for level '{level}'")
    changes.update({"updatedBy": user["_id"], "updatedAt": datetime.now(timezone.utc)})
    result = await database.assessments.find_one_and_update({"_id": identifier}, {"$set": changes}, return_document=True)
    return {"success": True, "message": "Assessment updated successfully", "data": json_safe(result)}


@router.put("/assessments/{assessment_id}/toggle-status")
async def toggle_assessment(assessment_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    identifier = object_id(assessment_id, "assessment id")
    current = await database.assessments.find_one({"_id": identifier})
    if not current:
        raise HTTPException(404, "Assessment not found")
    active = not current.get("isActive", False)
    if active and await database.assessments.find_one({"_id": {"$ne": identifier}, "level": current.get("level"), "isActive": True}):
        raise HTTPException(409, "Another active assessment already exists for this level")
    result = await database.assessments.find_one_and_update({"_id": identifier}, {"$set": {"isActive": active, "updatedBy": user["_id"], "updatedAt": datetime.now(timezone.utc)}}, return_document=True)
    return {"success": True, "message": "Assessment status updated", "data": json_safe(result)}


@router.delete("/assessments/{assessment_id}")
async def delete_assessment(assessment_id: str, database: AsyncIOMotorDatabase = Depends(get_database)):
    result = await database.assessments.delete_one({"_id": object_id(assessment_id, "assessment id")})
    if result.deleted_count == 0:
        raise HTTPException(404, "Assessment not found")
    return {"success": True, "message": "Assessment deleted successfully"}


async def _flatten_requests(database: AsyncIOMotorDatabase, state: str):
    caregivers = await database.users.find({"role": "caregiver", "expertRequests.status": state}).to_list(length=None)
    items = []
    for caregiver in caregivers:
        for request in caregiver.get("expertRequests", []):
            if request.get("status") != state:
                continue
            expert = await database.users.find_one({"_id": request.get("expertId")}, {"name": 1, "email": 1})
            items.append({"caregiverId": caregiver["_id"], "caregiverName": caregiver.get("name"), "caregiverEmail": caregiver.get("email"), "expertId": request.get("expertId"), "expertName": expert.get("name") if expert else None, "expertEmail": expert.get("email") if expert else None, "requestedAt": request.get("requestedAt"), "requestId": request.get("_id")})
    return items


@router.get("/expert-requests")
async def get_expert_requests(database: AsyncIOMotorDatabase = Depends(get_database)):
    items = await _flatten_requests(database, "pending")
    return {"success": True, "count": len(items), "data": json_safe(items)}


@router.get("/expert-assignments")
async def get_expert_assignments(database: AsyncIOMotorDatabase = Depends(get_database)):
    items = await _flatten_requests(database, "approved")
    return {"success": True, "count": len(items), "data": json_safe(items)}


async def _change_request(caregiver_id: str, expert_id: str, new_status: str, database: AsyncIOMotorDatabase):
    caregiver_oid = object_id(caregiver_id, "caregiver id")
    expert_oid = object_id(expert_id, "expert id")
    caregiver = await database.users.find_one({"_id": caregiver_oid, "role": "caregiver"})
    if not caregiver:
        raise HTTPException(404, "Caregiver not found")
    requests = caregiver.get("expertRequests", [])
    found = False
    for request in requests:
        if request.get("expertId") == expert_oid and request.get("status") == "pending":
            request["status"] = new_status
            found = True
            break
    if not found:
        raise HTTPException(404, "Pending request not found")
    await database.users.update_one({"_id": caregiver_oid}, {"$set": {"expertRequests": requests}})


@router.put("/expert-requests/{caregiver_id}/{expert_id}/approve")
async def approve_expert(caregiver_id: str, expert_id: str, database: AsyncIOMotorDatabase = Depends(get_database)):
    await _change_request(caregiver_id, expert_id, "approved", database)
    return {"success": True, "message": "Expert assignment approved"}


@router.put("/expert-requests/{caregiver_id}/{expert_id}/reject")
async def reject_expert(caregiver_id: str, expert_id: str, database: AsyncIOMotorDatabase = Depends(get_database)):
    await _change_request(caregiver_id, expert_id, "rejected", database)
    return {"success": True, "message": "Expert request rejected"}


@router.delete("/expert-assignments/{caregiver_id}/{expert_id}")
async def remove_assignment(caregiver_id: str, expert_id: str, database: AsyncIOMotorDatabase = Depends(get_database)):
    caregiver_oid = object_id(caregiver_id, "caregiver id")
    expert_oid = object_id(expert_id, "expert id")
    caregiver = await database.users.find_one({"_id": caregiver_oid, "role": "caregiver"})
    if not caregiver:
        raise HTTPException(404, "Caregiver not found")
    updated = [request for request in caregiver.get("expertRequests", []) if not (request.get("expertId") == expert_oid and request.get("status") == "approved")]
    if len(updated) == len(caregiver.get("expertRequests", [])):
        raise HTTPException(404, "Assignment not found")
    await database.users.update_one({"_id": caregiver_oid}, {"$set": {"expertRequests": updated}})
    return {"success": True, "message": "Expert assignment removed"}