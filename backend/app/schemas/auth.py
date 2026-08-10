from pydantic import BaseModel, EmailStr

from app.models.user import UserRole


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class MfaRequiredResponse(BaseModel):
    mfa_required: bool = True
    mfa_token: str


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    role: UserRole = UserRole.NETWORK_ENGINEER

    def sanitized_role(self) -> UserRole:
        """Roles a caller may grant *themselves* via public /auth/register.

        NETWORK_ADMIN and SECURITY are privileged roles (device
        create/delete, credential writes, JIT approval, config push,
        compliance/RBAC visibility) and must never be self-assigned --
        anyone hitting the open registration endpoint with
        role=network_admin in the body would otherwise get full control
        of every managed device. Those two roles can only be granted by
        an existing NETWORK_ADMIN via PATCH /auth/users/{id}/role.
        """
        if self.role in (UserRole.NETWORK_ADMIN, UserRole.SECURITY):
            return UserRole.NETWORK_ENGINEER
        return self.role


class UserRoleUpdate(BaseModel):
    role: UserRole


class RefreshRequest(BaseModel):
    refresh_token: str


class MfaSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaCodeRequest(BaseModel):
    code: str


class MfaVerifyRequest(BaseModel):
    mfa_token: str
    code: str


class MfaDisableRequest(BaseModel):
    password: str
