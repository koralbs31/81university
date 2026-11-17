import { getAPIUrl } from '@services/config/config';
/* ------------------------------------------------------------------------ */
/* Types (mirror apps/api/src/services/messaging)                            */
/* ------------------------------------------------------------------------ */
export type UserSummary = {
    id: number;
    user_uuid: string;
    username: string;
    first_name: string;
    last_name: string;
    avatar_image: string;
};
export type Sender = ({
    type: 'user';
} & UserSummary);
export type Reaction = {
    emoji: string;
    count: number;
    user_ids: number[];
    me: boolean;
};
export type CourseCard = {
    course_uuid: string;
    name: string;
    description: string;
    thumbnail_image: string;
};
export type MessageAttachment = {
    course?: CourseCard;
    follow_ups?: string[];
    /** course_uuid -> card, for ```course blocks an agent wrote */
    courses?: Record<string, CourseCard>;
    user_ids?: number[];
    for_user_id?: number;
    source?: {
        conversation_uuid: string;
        message_uuid: string;
        snippet: string;
    };
};
export type MessageKind = 'text' | 'announcement' | 'course_share' | 'system';
export type ChatMessage = {
    id: number;
    message_uuid: string;
    conversation_uuid: string;
    kind: MessageKind;
    content: string;
    attachment: MessageAttachment | null;
    reply_to: {
        message_uuid: string;
        content: string;
        kind: MessageKind;
        deleted: boolean;
        sender: Sender | null;
    } | null;
    sender: Sender | null;
    reactions: Reaction[];
    status: 'complete' | 'streaming' | 'error';
    edited_at: string | null;
    deleted: boolean;
    creation_date: string;
};
export type ConversationKind = 'dm' | 'group' | 'broadcast';
export type ConversationMemberSummary = UserSummary & {
    role: 'owner' | 'admin' | 'member';
    last_read_message_id: number | null;
};
export type Conversation = {
    id: number;
    conversation_uuid: string;
    org_id: number;
    kind: ConversationKind;
    title: string;
    emoji: string;
    course: CourseCard | null;
    member_count: number;
    members: ConversationMemberSummary[];
    my_role: 'owner' | 'admin' | 'member' | null;
    muted: boolean;
    pinned: boolean;
    archived: boolean;
    last_read_message_id: number;
    unread_count: number;
    last_message: ChatMessage | null;
    last_message_at: string;
    created_by: number | null;
    creation_date: string;
};
export type PresenceState = 'online' | 'away' | 'dnd' | 'offline';
export type Presence = {
    user_id: number;
    online: boolean;
    state: PresenceState;
    status_emoji: string;
    status_text: string;
    last_seen_at: string | null;
};
export type DirectoryPerson = UserSummary & {
    presence: Presence;
};
export type Availability = 'auto' | 'away' | 'dnd' | 'invisible';
export type EmailMode = 'all' | 'direct' | 'off';
export type MyStatus = Presence & {
    availability: Availability;
    status_expires_at: string | null;
    allow_dms: boolean;
    email_mode: EmailMode;
    email_delay_minutes: 15 | 60 | 240 | 1440;
    email_broadcasts: boolean;
    desktop_notifications: boolean;
    sounds: boolean;
    read_receipts: boolean;
    typing_indicators: boolean;
};
export type BroadcastTarget = CourseCard & {
    course_uuid: string | null;
    learners: number;
};
/** Events on the live stream. */
export type MessagingEvent = {
    type: 'hello';
    user_id: number;
    me: MyStatus;
    online_user_ids: number[];
} | {
    type: 'message.new';
    conversation_uuid: string;
    message: ChatMessage;
} | {
    type: 'message.updated';
    conversation_uuid: string;
    message: ChatMessage;
} | {
    type: 'message.deleted';
    conversation_uuid: string;
    message_uuid: string;
} | {
    type: 'reaction';
    conversation_uuid: string;
    message_uuid: string;
    reactions: Reaction[];
    actor_id: number;
    emoji: string;
    added: boolean;
} | {
    type: 'typing';
    conversation_uuid: string;
    user: UserSummary;
    is_typing: boolean;
} | {
    type: 'read';
    conversation_uuid: string;
    user_id: number;
    message_id: number;
} | ({
    type: 'presence';
} & Presence) | {
    type: 'conversation.new';
    conversation_uuid: string;
} | {
    type: 'conversation.updated';
    conversation_uuid: string;
} | {
    type: 'error';
    message: string;
};
/* ------------------------------------------------------------------------ */
/* HTTP                                                                     */
/* ------------------------------------------------------------------------ */
export class MessagingError extends Error {
    status: number;
    constructor(message: string, status: number) {
        super(message);
        this.status = status;
    }
}
async function call<T>(path: string, token: string, init: {
    method?: string;
    body?: any;
} = {}): Promise<T> {
    const res = await fetch(`${getAPIUrl()}messaging/${path}`, {
        method: init.method || 'GET',
        headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
        },
        credentials: 'include',
        body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
    });
    if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        const detail = typeof data?.detail === 'string' ? data.detail : data?.detail?.message;
        throw new MessagingError(detail || `Request failed (${res.status})`, res.status);
    }
    return res.json();
}
const q = (params: Record<string, string | number | undefined | null>) => Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
    .join('&');
// Conversations
export const listConversations = (orgId: number, token: string) => call<Conversation[]>(`conversations?${q({ org_id: orgId })}`, token);
export const getConversation = (uuid: string, token: string) => call<Conversation>(`conversations/${uuid}`, token);
export const createConversation = (body: {
    org_id: number;
    kind: 'dm' | 'group';
    user_uuids: string[];
    title?: string;
    emoji?: string;
}, token: string) => call<Conversation>('conversations', token, { method: 'POST', body });
export const updateConversation = (uuid: string, body: Partial<{
    title: string;
    emoji: string;
    muted: boolean;
    pinned: boolean;
    archived: boolean;
}>, token: string) => call<Conversation>(`conversations/${uuid}`, token, { method: 'PATCH', body });
export const addMembers = (uuid: string, userUuids: string[], token: string) => call<Conversation>(`conversations/${uuid}/members`, token, { method: 'POST', body: { user_uuids: userUuids } });
export const leaveConversation = (uuid: string, token: string) => call<{
    left: boolean;
}>(`conversations/${uuid}/members/me`, token, { method: 'DELETE' });
export const listMessages = (uuid: string, token: string, beforeId?: number, limit = 50) => call<{
    messages: ChatMessage[];
    has_more: boolean;
}>(`conversations/${uuid}/messages?${q({ before_id: beforeId, limit })}`, token);
export type SendResult = {
    message: ChatMessage;
};
export const sendMessage = (uuid: string, body: {
    content: string;
    course_uuid?: string;
    reply_to_uuid?: string;
}, token: string) => call<SendResult>(`conversations/${uuid}/messages`, token, { method: 'POST', body });
export const sendTyping = (uuid: string, isTyping: boolean, token: string) => call<{
    ok: boolean;
}>(`conversations/${uuid}/typing`, token, { method: 'POST', body: { is_typing: isTyping } });
export const markRead = (uuid: string, token: string, messageUuid?: string) => call<{
    last_read_message_id: number;
}>(`conversations/${uuid}/read`, token, {
    method: 'POST',
    body: { message_uuid: messageUuid ?? null },
});
// Messages
export const editMessage = (uuid: string, content: string, token: string) => call<ChatMessage>(`messages/${uuid}`, token, { method: 'PATCH', body: { content } });
export const deleteMessage = (uuid: string, token: string) => call<{
    deleted: boolean;
}>(`messages/${uuid}`, token, { method: 'DELETE' });
export const toggleReaction = (uuid: string, emoji: string, token: string) => call<{
    reactions: Reaction[];
}>(`messages/${uuid}/reactions`, token, { method: 'POST', body: { emoji } });
export const searchMessages = (orgId: number, query: string, token: string) => call<ChatMessage[]>(`search?${q({ org_id: orgId, q: query })}`, token);
export const getUnread = (orgId: number, token: string) => call<{
    unread: number;
    conversations: number;
}>(`unread?${q({ org_id: orgId })}`, token);
// People
export const getDirectory = (orgId: number, query: string, token: string, limit = 40) => call<DirectoryPerson[]>(`directory?${q({ org_id: orgId, q: query, limit })}`, token);
export const getPresence = (orgId: number, userIds: number[], token: string) => call<Presence[]>(`presence?${q({ org_id: orgId, user_ids: userIds.join(',') })}`, token);
export const getMe = (orgId: number, token: string) => call<MyStatus>(`me?${q({ org_id: orgId })}`, token);
export const updateMe = (orgId: number, body: Partial<MyStatus>, token: string) => call<MyStatus>(`me?${q({ org_id: orgId })}`, token, { method: 'PUT', body });
// Broadcasts
export const getBroadcastTargets = (orgId: number, token: string) => call<BroadcastTarget[]>(`broadcast/targets?${q({ org_id: orgId })}`, token);
export const sendBroadcast = (body: {
    org_id: number;
    course_uuid: string | null;
    content: string;
    share_course?: boolean;
}, token: string) => call<{
    conversation: Conversation;
    message: ChatMessage;
    recipients: number;
}>('broadcast', token, {
    method: 'POST',
    body,
});
/* ------------------------------------------------------------------------ */
/* Live stream                                                              */
/* ------------------------------------------------------------------------ */
/**
 * Open the SSE stream with fetch (EventSource can't send the bearer token).
 * Resolves when the stream ends; abort the signal to close it.
 */
export async function openMessagingStream(orgId: number, token: string, onEvent: (event: MessagingEvent) => void, signal: AbortSignal): Promise<void> {
    const res = await fetch(`${getAPIUrl()}messaging/stream?${q({ org_id: orgId })}`, {
        headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' },
        credentials: 'include',
        signal,
    });
    if (!res.ok || !res.body) {
        throw new MessagingError(`Stream failed (${res.status})`, res.status);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done)
            break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop() || '';
        for (const frame of frames) {
            for (const line of frame.split('\n')) {
                if (!line.startsWith('data: '))
                    continue;
                try {
                    onEvent(JSON.parse(line.slice(6)));
                }
                catch {
                    // A malformed frame shouldn't take the stream down.
                }
            }
        }
    }
}
/* ------------------------------------------------------------------------ */
/* Display helpers                                                          */
/* ------------------------------------------------------------------------ */
export function displayName(u: Pick<UserSummary, 'first_name' | 'last_name' | 'username'> | null | undefined) {
    if (!u)
        return '';
    const full = `${u.first_name || ''} ${u.last_name || ''}`.trim();
    return full || u.username;
}
/** The title a conversation should show to this viewer. */
export function conversationTitle(c: Conversation, viewerId: number | undefined): string {
    if (c.kind === 'dm') {
        const other = c.members.find((m) => m.id !== viewerId);
        return other ? displayName(other) : 'Direct message';
    }
    return c.title || 'Conversation';
}
export function otherMember(c: Conversation, viewerId: number | undefined) {
    return c.kind === 'dm' ? c.members.find((m) => m.id !== viewerId) ?? null : null;
}
