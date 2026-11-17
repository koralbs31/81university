from typing import Optional
from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

class Conversation(SQLModel, table=True):
    __table_args__ = (Index('ix_conversation_org_kind', 'org_id', 'kind'),)
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_uuid: str = Field(default='', index=True)
    org_id: int = Field(sa_column=Column(BigInteger, ForeignKey('organization.id', ondelete='CASCADE'), nullable=False, index=True))
    kind: str = 'dm'
    title: str = ''
    emoji: str = ''
    dm_key: Optional[str] = Field(default=None, index=True)
    course_id: Optional[int] = Field(default=None, sa_column=Column(Integer, ForeignKey('course.id', ondelete='CASCADE'), nullable=True, index=True))
    created_by: Optional[int] = Field(default=None, sa_column=Column(Integer, ForeignKey('user.id', ondelete='SET NULL'), nullable=True))
    last_message_at: str = ''
    creation_date: str = ''
    update_date: str = ''

class ConversationMember(SQLModel, table=True):
    __table_args__ = (UniqueConstraint('conversation_id', 'user_id', name='uq_conversationmember_conv_user'),)
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(sa_column=Column(Integer, ForeignKey('conversation.id', ondelete='CASCADE'), nullable=False, index=True))
    user_id: int = Field(sa_column=Column(Integer, ForeignKey('user.id', ondelete='CASCADE'), nullable=False, index=True))
    role: str = 'member'
    last_read_message_id: int = 0
    last_emailed_message_id: int = 0
    muted: bool = False
    pinned: bool = False
    archived: bool = False
    joined_at: str = ''

class Message(SQLModel, table=True):
    __table_args__ = (Index('ix_message_conversation_id_id', 'conversation_id', 'id'),)
    id: Optional[int] = Field(default=None, primary_key=True)
    message_uuid: str = Field(default='', index=True)
    conversation_id: int = Field(sa_column=Column(Integer, ForeignKey('conversation.id', ondelete='CASCADE'), nullable=False))
    org_id: int = Field(sa_column=Column(BigInteger, ForeignKey('organization.id', ondelete='CASCADE'), nullable=False, index=True))
    sender_user_id: Optional[int] = Field(default=None, sa_column=Column(Integer, ForeignKey('user.id', ondelete='SET NULL'), nullable=True, index=True))
    kind: str = 'text'
    content: str = Field(default='', sa_column=Column(Text, nullable=False, server_default=''))
    attachment: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    reply_to_id: Optional[int] = Field(default=None, sa_column=Column(Integer, ForeignKey('message.id', ondelete='SET NULL'), nullable=True))
    status: str = 'complete'
    edited_at: Optional[str] = None
    deleted_at: Optional[str] = None
    creation_date: str = ''

class MessageReaction(SQLModel, table=True):
    __table_args__ = (UniqueConstraint('message_id', 'user_id', 'emoji', name='uq_messagereaction_msg_user_emoji'),)
    id: Optional[int] = Field(default=None, primary_key=True)
    message_id: int = Field(sa_column=Column(Integer, ForeignKey('message.id', ondelete='CASCADE'), nullable=False, index=True))
    user_id: int = Field(sa_column=Column(Integer, ForeignKey('user.id', ondelete='CASCADE'), nullable=False))
    emoji: str
    creation_date: str = ''

class MessagingProfile(SQLModel, table=True):
    """Per-user presence preferences: a custom status and how they show up."""
    __table_args__ = (UniqueConstraint('user_id', name='uq_messagingprofile_user'),)
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(sa_column=Column(Integer, ForeignKey('user.id', ondelete='CASCADE'), nullable=False))
    availability: str = 'auto'
    status_emoji: str = ''
    status_text: str = ''
    status_expires_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    allow_dms: bool = True
    email_mode: str = 'direct'
    email_delay_minutes: int = 60
    email_broadcasts: bool = True
    desktop_notifications: bool = True
    sounds: bool = True
    read_receipts: bool = True
    typing_indicators: bool = True
    update_date: str = ''
