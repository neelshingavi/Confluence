from pydantic import BaseModel, Field, ConfigDict
from typing import Literal

class LoginInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=300)

class PasswordChangeInput(BaseModel):
    oldPassword: str = Field(min_length=1, max_length=300)
    newPassword: str = Field(min_length=12, max_length=300)

class ScanInput(BaseModel):
    qrToken: str = Field(min_length=12, max_length=1000)
    clientScanId: str = Field(min_length=36, max_length=36)

class StaffCreateInput(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=300)
    role: Literal["ADMIN", "CHECKIN_STAFF", "VIEWER"]

class TemplatePlacements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qr: dict
    name: dict
    year: dict
    phone: dict

class ReverseInput(BaseModel):
    reason: str = Field(min_length=8, max_length=300)
