import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet
from fastapi import Cookie, Depends, HTTPException, Request
import jwt
from pwdlib import PasswordHash
from sqlalchemy.orm import Session
from .config import get_settings
from .database import SessionLocal
from .models import User


settings = get_settings()
passwords = PasswordHash.recommended()


def _fernet() -> Fernet:
    if settings.data_encryption_key:
        return Fernet(settings.data_encryption_key.encode())
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.jwt_secret.encode()).digest())
    return Fernet(key)


def encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_text(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def encrypt_json(value: object) -> str:
    return encrypt_text(json.dumps(value, ensure_ascii=False))


def decrypt_json(value: str) -> object:
    return json.loads(decrypt_text(value))


def hash_password(value: str) -> str:
    return passwords.hash(value)


def verify_password(value: str, hashed: str) -> bool:
    return passwords.verify(value, hashed)


def issue_token(user: User) -> str:
    payload = {"sub": user.id, "org": user.organization_id, "role": user.role, "exp": datetime.now(timezone.utc) + timedelta(hours=12)}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, zorg_session: str | None = Cookie(default=None), db: Session = Depends(get_db)) -> User:
    if not zorg_session:
        raise HTTPException(401, "Log eerst in")
    try:
        payload = jwt.decode(zorg_session, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "Sessie ongeldig") from exc
    user = db.get(User, payload["sub"])
    if not user or not user.active:
        raise HTTPException(401, "Gebruiker niet actief")
    password_setup_paths = {"/api/me", "/api/me/password", "/api/logout"}
    if user.must_change_password and request.url.path not in password_setup_paths:
        raise HTTPException(428, "Kies eerst een eigen wachtwoord")
    return user
