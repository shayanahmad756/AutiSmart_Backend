from datetime import datetime, timedelta, timezone
import re

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from .core import (
    create_token,
    get_current_user,
    get_database,
    hash_password,
    json_safe,
    new_otp,
    public_user,
    send_otp_email,
    verify_password,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    name = str(payload.get("name", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    role = payload.get("role") or "caregiver"
    if not name or not email or not password:
        raise HTTPException(400, "Name, email, and password are required")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(400, "Please provide a valid email")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if role not in {"caregiver", "expert"}:
        raise HTTPException(400, "Invalid role")
    if await database.users.find_one({"email": email}):
        raise HTTPException(409, "Email already registered")

    otp = new_otp()
    user = {
        "name": name,
        "email": email,
        "password": hash_password(password),
        "role": role,
        "isVerified": False,
        "expertRequests": [],
        "otp": otp,
        "otpExpires": datetime.now(timezone.utc) + timedelta(minutes=10),
        "createdAt": datetime.now(timezone.utc),
        "updatedAt": datetime.now(timezone.utc),
    }
    result = await database.users.insert_one(user)
    user["_id"] = result.inserted_id
    try:
        await send_otp_email(email, otp, name)
    except Exception as error:
        await database.users.delete_one({"_id": result.inserted_id})
        raise HTTPException(503, f"Failed to send verification email: {error}") from error
    return {
        "success": True,
        "message": "Registration successful! Please check your email for the verification code.",
        "data": public_user(user),
    }


@router.post("/verify-otp")
async def verify_otp(payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    email = str(payload.get("email", "")).strip().lower()
    otp = str(payload.get("otp", "")).strip()
    if not email or not otp:
        raise HTTPException(400, "Email and OTP are required")
    user = await database.users.find_one({"email": email})
    if not user:
        raise HTTPException(404, "User not found")
    if user.get("isVerified"):
        raise HTTPException(400, "User already verified")
    if not user.get("otp") or not user.get("otpExpires"):
        raise HTTPException(400, "No OTP found. Please request a new one.")
    expires = user["otpExpires"]
    if expires.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc) if expires.tzinfo is None else expires < datetime.now(timezone.utc):
        raise HTTPException(400, "OTP has expired. Please request a new one.")
    if user["otp"] != otp:
        raise HTTPException(400, "Invalid OTP")
    await database.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"isVerified": True, "updatedAt": datetime.now(timezone.utc)}, "$unset": {"otp": "", "otpExpires": "", "otpPurpose": ""}},
    )
    return {
        "success": True,
        "message": "Email verified successfully",
        "data": {"token": create_token(user["_id"]), "user": public_user(user)},
    }


@router.post("/login")
async def login(payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if not email or not password:
        raise HTTPException(400, "Email and password are required")
    user = await database.users.find_one({"email": email})
    if not user or not verify_password(password, user.get("password")):
        raise HTTPException(401, "Invalid credentials")
    if not user.get("isVerified"):
        raise HTTPException(401, "Please verify your email before logging in")
    return {"success": True, "message": "Login successful", "data": {"token": create_token(user["_id"]), "user": public_user(user)}}


@router.post("/resend-otp")
async def resend_otp(payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    email = str(payload.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(400, "Email is required")
    user = await database.users.find_one({"email": email})
    if not user:
        raise HTTPException(404, "User not found")
    if user.get("isVerified"):
        raise HTTPException(400, "User already verified")
    otp = new_otp()
    await database.users.update_one({"_id": user["_id"]}, {"$set": {"otp": otp, "otpExpires": datetime.now(timezone.utc) + timedelta(minutes=10)}})
    await send_otp_email(email, otp, user.get("name", "User"))
    return {"success": True, "message": "OTP sent successfully"}


@router.post("/forgot-password")
async def forgot_password(payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    email = str(payload.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(400, "Email is required")
    user = await database.users.find_one({"email": email})
    if not user:
        raise HTTPException(404, "User not found")
    otp = new_otp()
    await database.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"otp": otp, "otpPurpose": "password-reset", "otpExpires": datetime.now(timezone.utc) + timedelta(minutes=10)}},
    )
    await send_otp_email(email, otp, user.get("name", "User"), password_reset=True)
    return {"success": True, "message": "Password reset code sent successfully"}


@router.post("/reset-password")
async def reset_password(payload: dict, database: AsyncIOMotorDatabase = Depends(get_database)):
    email = str(payload.get("email", "")).strip().lower()
    otp = str(payload.get("otp", "")).strip()
    password = str(payload.get("newPassword", ""))
    if not email or not otp or not password:
        raise HTTPException(400, "Email, OTP, and new password are required")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    user = await database.users.find_one({"email": email, "otpPurpose": "password-reset"})
    if not user or user.get("otp") != otp:
        raise HTTPException(400, "Invalid or expired password reset code")
    expires = user.get("otpExpires")
    if not expires or (expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires) < datetime.now(timezone.utc):
        raise HTTPException(400, "Invalid or expired password reset code")
    await database.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password": hash_password(password), "updatedAt": datetime.now(timezone.utc)}, "$unset": {"otp": "", "otpExpires": "", "otpPurpose": ""}},
    )
    return {"success": True, "message": "Password reset successfully"}


@router.get("/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    return {
        "success": True,
        "data": json_safe({key: user.get(key) for key in ("_id", "name", "email", "phoneNumber", "phone", "role", "isVerified", "createdAt")}),
    }


@router.put("/change-password")
async def change_password(payload: dict, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    current = str(payload.get("currentPassword", ""))
    new = str(payload.get("newPassword", ""))
    if not current or not new:
        raise HTTPException(400, "Current password and new password are required")
    if not verify_password(current, user.get("password")):
        raise HTTPException(400, "Current password is incorrect")
    if len(new) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")
    await database.users.update_one({"_id": user["_id"]}, {"$set": {"password": hash_password(new)}})
    return {"success": True, "message": "Password changed successfully"}


@router.get("/experts")
async def get_experts(database: AsyncIOMotorDatabase = Depends(get_database), _user: dict = Depends(get_current_user)):
    experts = await database.users.find({"role": "expert"}, {"name": 1, "email": 1, "role": 1, "createdAt": 1}).to_list(length=None)
    return {"success": True, "data": json_safe(experts)}


@router.post("/expert-request", status_code=201)
async def request_expert(payload: dict, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    if user.get("role") != "caregiver":
        raise HTTPException(403, "Only caregivers can request experts")
    expert_id = payload.get("expertId")
    if not expert_id:
        raise HTTPException(400, "Expert ID is required")
    expert = await database.users.find_one({"_id": ObjectId(expert_id), "role": "expert"})
    if not expert:
        raise HTTPException(404, "Expert not found")
    requests = user.get("expertRequests", [])
    existing = next((item for item in requests if str(item.get("expertId")) == str(expert["_id"]) and item.get("status") in {"pending", "approved"}), None)
    if existing:
        message = "Request already pending" if existing["status"] == "pending" else "Already assigned to this expert"
        raise HTTPException(400, message)
    requests.append({"expertId": expert["_id"], "status": "pending", "requestedAt": datetime.now(timezone.utc), "_id": ObjectId()})
    await database.users.update_one({"_id": user["_id"]}, {"$set": {"expertRequests": requests}})
    return {"success": True, "message": "Expert assignment request sent successfully"}


@router.delete("/expert-request/{expert_id}")
async def cancel_expert_request(expert_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    if user.get("role") != "caregiver":
        raise HTTPException(403, "Only caregivers can cancel requests")
    requests = user.get("expertRequests", [])
    updated = [item for item in requests if not (str(item.get("expertId")) == expert_id and item.get("status") == "pending")]
    if len(updated) == len(requests):
        raise HTTPException(404, "Pending request not found")
    await database.users.update_one({"_id": user["_id"]}, {"$set": {"expertRequests": updated}})
    return {"success": True, "message": "Request cancelled"}


@router.get("/my-expert-requests")
async def my_expert_requests(user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    if user.get("role") != "caregiver":
        raise HTTPException(403, "Only caregivers can view expert requests")
    result = []
    for request in user.get("expertRequests", []):
        expert = await database.users.find_one({"_id": request.get("expertId")}, {"name": 1, "email": 1})
        result.append({**request, "expertId": expert or request.get("expertId")})
    return {"success": True, "data": json_safe(result)}


@router.get("/notifications")
async def notifications(user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    role = user.get("role")
    output = []
    if role == "caregiver":
        for request in user.get("expertRequests", []):
            if request.get("status") not in {"approved", "rejected"}:
                continue
            expert = await database.users.find_one({"_id": request.get("expertId")}, {"name": 1})
            name = expert.get("name") if expert else "An expert"
            output.append({"id": f"{request.get('_id', request.get('expertId'))}-{request['status']}", "message": f"{name} has been assigned to you" if request["status"] == "approved" else f"Your request for {name} was rejected", "type": "success" if request["status"] == "approved" else "danger", "createdAt": request.get("requestedAt")})
    elif role == "admin":
        caregivers = await database.users.find({"role": "caregiver", "expertRequests.status": "pending"}).to_list(length=None)
        for caregiver in caregivers:
            for request in caregiver.get("expertRequests", []):
                if request.get("status") == "pending":
                    expert = await database.users.find_one({"_id": request.get("expertId")}, {"name": 1})
                    output.append({"id": f"{caregiver['_id']}-{request.get('expertId')}-pending", "message": f"{caregiver['name']} requested assignment to {expert.get('name', 'an expert') if expert else 'an expert'}", "type": "warning", "createdAt": request.get("requestedAt")})
    elif role == "expert":
        caregivers = await database.users.find({"role": "caregiver", "expertRequests": {"$elemMatch": {"expertId": user["_id"], "status": "approved"}}}, {"name": 1, "expertRequests": 1}).to_list(length=None)
        for caregiver in caregivers:
            request = next((item for item in caregiver.get("expertRequests", []) if str(item.get("expertId")) == str(user["_id"]) and item.get("status") == "approved"), {})
            output.append({"id": f"{caregiver['_id']}-assigned", "message": f"You are assigned to {caregiver['name']}'s children", "type": "info", "createdAt": request.get("requestedAt")})
    output.sort(key=lambda item: item.get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {"success": True, "data": json_safe(output)}