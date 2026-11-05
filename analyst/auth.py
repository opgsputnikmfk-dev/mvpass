"""Named logins with salted password hashes. No plaintext credentials in source."""
import base64
import hashlib
import hmac
import json
import re
import secrets

ITERATIONS=600000

def password_hash(password):
    salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac("sha256",password.encode(),salt,ITERATIONS)
    return "$".join(("pbkdf2_sha256",str(ITERATIONS),base64.b64encode(salt).decode(),base64.b64encode(digest).decode()))

def check_password(password,encoded):
    try:
        alg,n,salt,digest=encoded.split("$");n=int(n)
        if alg!="pbkdf2_sha256" or not 100000<=n<=2000000:return False
        expected=base64.b64decode(digest,validate=True);salt=base64.b64decode(salt,validate=True)
        if len(salt)<16 or len(expected)!=32:return False
        actual=hashlib.pbkdf2_hmac("sha256",password.encode(),salt,n)
        return hmac.compare_digest(actual,expected)
    except (ValueError,TypeError,AttributeError):return False

def load_users(raw):
    if not raw:return {}
    data=json.loads(raw)
    if not isinstance(data,dict) or not 1<=len(data)<=10:raise ValueError("Неверная конфигурация пользователей")
    seen=set()
    for name,record in data.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}",name) or name.casefold() in seen:
            raise ValueError("Неверное имя пользователя")
        seen.add(name.casefold())
        if not isinstance(record,dict) or set(record)!={"password_hash","role"} or record["role"] not in ("admin","user"):
            raise ValueError("Неверная роль пользователя")
        encoded=record["password_hash"]
        if not isinstance(encoded,str) or not encoded.startswith("pbkdf2_sha256$"):raise ValueError("Нужен хеш пароля")
    if data.get("Admin",{}).get("role")!="admin" or any(v["role"]=="admin" for k,v in data.items() if k!="Admin"):
        raise ValueError("Только Admin может быть администратором")
    return data

class NamedAuth:
    def __init__(self,records):
        self.records=records
        self.names={n.casefold():n for n in records}
        self.dummy=password_hash(secrets.token_urlsafe(24)) if records else ""
    def authenticate(self,username,password):
        if not isinstance(username,str) or not isinstance(password,str) or len(username)>64 or len(password)>1024:return None
        name=self.names.get(username.strip().casefold())
        encoded=self.records[name]["password_hash"] if name else self.dummy
        ok=check_password(password,encoded)
        return name if ok and name else None
