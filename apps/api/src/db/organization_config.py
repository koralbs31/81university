from typing import Literal, Optional
from pydantic import BaseModel, field_validator
from sqlalchemy import JSON, BigInteger, Column, ForeignKey
from sqlmodel import Field, SQLModel

class CourseOrgConfig(BaseModel):
    enabled: bool = True
    limit: int = 10

class MemberOrgConfig(BaseModel):
    enabled: bool = True
    signup_mode: Literal['open', 'inviteOnly'] = 'open'
    admin_limit: int = 1
    limit: int = 10

class UserGroupOrgConfig(BaseModel):
    enabled: bool = True
    limit: int = 10

class StorageOrgConfig(BaseModel):
    enabled: bool = True
    limit: int = 10

class AssignmentOrgConfig(BaseModel):
    enabled: bool = False
    limit: int = 10

class DiscussionOrgConfig(BaseModel):
    enabled: bool = True
    limit: int = 10

class CommunitiesOrgConfig(BaseModel):
    enabled: bool = True

class AnalyticsOrgConfig(BaseModel):
    enabled: bool = True
    limit: int = 10

class APIOrgConfig(BaseModel):
    enabled: bool = True
    limit: int = 10

class OrgFeatureConfig(BaseModel):
    courses: CourseOrgConfig = CourseOrgConfig()
    members: MemberOrgConfig = MemberOrgConfig()
    usergroups: UserGroupOrgConfig = UserGroupOrgConfig()
    storage: StorageOrgConfig = StorageOrgConfig()
    assignments: AssignmentOrgConfig = AssignmentOrgConfig()
    discussions: DiscussionOrgConfig = DiscussionOrgConfig()
    communities: CommunitiesOrgConfig = CommunitiesOrgConfig()
    analytics: AnalyticsOrgConfig = AnalyticsOrgConfig()
    api: APIOrgConfig = APIOrgConfig()

class MembersAdminToggle(BaseModel):
    disabled: bool = False
    signup_mode: Literal['open', 'inviteOnly'] = 'open'

class FeatureAdminToggle(BaseModel):
    disabled: bool = False
    org_disabled: bool = False

class SecurityAdminToggle(BaseModel):
    """Org-wide security policy.

    Lives in the config JSON blob, so enabling it needs no migration.

    ``require_2fa_enabled_at`` is the anchor the grace period counts from. Each
    member's personal deadline is
    ``max(require_2fa_enabled_at, their join date) + require_2fa_grace_days``;
    without the ``max`` a member who joins after the policy was set would land
    with an already-expired deadline and be locked out on their first day.
    """
    require_2fa: bool = False
    require_2fa_grace_days: int = 0
    require_2fa_enabled_at: Optional[str] = None
    exempt_external_auth: bool = True
    allowed_auth_methods: list[str] = Field(default_factory=lambda: ['password', 'magic_login', 'google', 'sso'])
    allow_central_session_sharing: bool = True

class AdminToggles(BaseModel):
    security: SecurityAdminToggle = SecurityAdminToggle()
    analytics: FeatureAdminToggle = FeatureAdminToggle()
    api: FeatureAdminToggle = FeatureAdminToggle()
    communities: FeatureAdminToggle = FeatureAdminToggle()
    members: MembersAdminToggle = MembersAdminToggle()
    paths: FeatureAdminToggle = FeatureAdminToggle()
    messaging: FeatureAdminToggle = FeatureAdminToggle()
    live_sessions: FeatureAdminToggle = FeatureAdminToggle()

class FeatureOverride(BaseModel):
    extra_limit: int = 0
    force_enabled: bool = False

class Overrides(BaseModel):
    note: str = ''

class AuthBrandingConfig(BaseModel):
    welcome_message: str = ''
    background_type: Literal['gradient', 'custom', 'unsplash'] = 'gradient'
    background_image: str = ''
    text_color: Literal['light', 'dark'] = 'light'
    unsplash_photographer_name: str = ''
    unsplash_photographer_url: str = ''
    unsplash_photo_url: str = ''

class GeneralCustomization(BaseModel):
    color: str = ''
    footer_text: str = ''
    favicon_image: str = ''
    square_logo_image: str = ''
    watermark: bool = True
    font: str = ''
    default_language: str = 'en'
    email_sender_name: str = ''

class SeoOrgConfig(BaseModel):
    default_meta_title_suffix: str = ''
    default_meta_description: str = ''
    default_og_image: str = ''
    google_site_verification: str = ''
    twitter_handle: str = ''
    noindex_communities: bool = False

class MenuLinkItem(BaseModel):
    type: str
    enabled: bool = True
    order: int = 0
    label: str = ''
    url: str = ''
    icon: str = ''

class MenuConfig(BaseModel):
    items: list[MenuLinkItem] = Field(default_factory=list)
SignupFieldType = Literal['text', 'textarea', 'select', 'checkbox', 'number', 'date']

class SignupFieldItem(BaseModel):
    """One admin-defined field shown on the org's signup form.

    Answers are stored on the user's ``extra_metadata`` JSON under ``key``.

    ``key`` is the JSON key and must stay stable: renaming it orphans every
    answer already collected, so the admin UI locks it after creation.

    NOTE: this config is served by the unauthenticated ``GET /orgs/slug/{slug}``
    endpoint (the public signup form needs it), so labels, help text and options
    are PUBLIC strings. Nothing sensitive belongs in them.
    """
    key: str
    label: str = ''
    type: SignupFieldType = 'text'
    required: bool = False
    enabled: bool = True
    order: int = 0
    help_text: str = ''
    placeholder: str = ''
    options: list[str] = Field(default_factory=list)
    max_length: Optional[int] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None

class SignupFieldsConfig(BaseModel):
    fields: list[SignupFieldItem] = Field(default_factory=list)

class CourseEndConfig(BaseModel):
    message: str = Field(default='', max_length=500)
    button_text: str = Field(default='', max_length=60)
    button_link: str = Field(default='', max_length=2048)

    @field_validator('message', 'button_text', 'button_link')
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator('button_link')
    @classmethod
    def _safe_link(cls, value: str) -> str:
        if not value:
            return value
        if value.startswith('/') and (not value.startswith('//')):
            return value
        if value.lower().startswith(('http://', 'https://')):
            return value
        raise ValueError('button_link must be an internal path or an http(s) URL')

class CustomizationConfig(BaseModel):
    general: GeneralCustomization = GeneralCustomization()
    auth_branding: AuthBrandingConfig = AuthBrandingConfig()
    seo: SeoOrgConfig = SeoOrgConfig()
    landing: dict = Field(default_factory=dict)
    menu: MenuConfig = MenuConfig()
    signup_fields: SignupFieldsConfig = SignupFieldsConfig()
    course_end: CourseEndConfig = CourseEndConfig()

class OrgGeneralConfig(BaseModel):
    enabled: bool = True
    color: str = ''
    footer_text: str = ''
    watermark: bool = True
    favicon_image: str = ''
    square_logo_image: str = ''
    auth_branding: AuthBrandingConfig = AuthBrandingConfig()

class OrgCloudConfig(BaseModel):
    plan: Literal['free', 'personal', 'personal-family', 'standard', 'pro', 'enterprise', 'oss'] = 'free'
    custom_domain: bool = False

class OrganizationConfigBase(BaseModel):
    config_version: str = '1.4'
    general: OrgGeneralConfig
    features: OrgFeatureConfig
    cloud: OrgCloudConfig
    landing: dict = Field(default_factory=dict)
    seo: SeoOrgConfig = SeoOrgConfig()

class OrganizationConfigV2Base(BaseModel):
    config_version: str = '2.0'
    active: bool = True
    plan: str = 'free'
    admin_toggles: AdminToggles = AdminToggles()
    overrides: dict = Field(default_factory=dict)
    customization: CustomizationConfig = CustomizationConfig()

class OrganizationConfig(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(sa_column=Column(BigInteger, ForeignKey('organization.id', ondelete='CASCADE'), index=True))
    config: dict = Field(default_factory=dict, sa_column=Column(JSON))
    creation_date: Optional[str] = None
    update_date: Optional[str] = None
