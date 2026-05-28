// static/js/chat.js

// -----------------------------
// Auth + header wiring
// -----------------------------
const LS_TOKEN = 'fa_access_token';
const LS_EMAIL = 'fa_email';
const PENDING_CHAT_MESSAGES_KEY = 'fa_pending_chat_messages_v1';
const MESSAGE_FEEDBACK_KEY = 'fa_message_feedback_v1';
const MESSAGE_FEEDBACK_BASE = '/chatbot/api/chats';

const FA_ENV = window.FA_ENV || 'local';

// Use proxy endpoints through FastAPI (avoids CORS issues)
// The FastAPI backend will forward these to the Django backend
const SESSIONS_URL   = '/proxy/chat/sessions/';
const LOG_TURN_URL   = '/proxy/chat/log-turn/';
const PDF_UPLOAD_URL = '/files/pdf';
const MAX_QUESTION_CHARS = 4000;

// Current auth token and email from login page / login.js
let authToken    = localStorage.getItem(LS_TOKEN);
let refreshToken = localStorage.getItem('fa_refresh_token') || null;

// Email: prefer the new key 'fa_user_email', fall back to legacy 'fa_email'
const storedUserEmail = localStorage.getItem('fa_user_email') || localStorage.getItem(LS_EMAIL);
const authEmail = storedUserEmail || '';
const DEBUG = false;

function debugLog(...args) {
    if (DEBUG) console.log(...args);
}

// If no token, send user back to login
if (!authToken) {
    window.location.href = "/";
}

// Decode JWT to get a reasonable display name (first_name / given_name / name)
function getDisplayNameFromToken(token, fallbackEmail) {
    if (!token) return fallbackEmail || '';

    try {
        const payloadBase64 = token.split('.')[1];
        if (!payloadBase64) return fallbackEmail || '';

        // Convert base64url -> base64 and pad so atob works
        const padded = payloadBase64
            .replace(/-/g, '+')
            .replace(/_/g, '/')
            .padEnd(Math.ceil(payloadBase64.length / 4) * 4, '=');

        const payload = JSON.parse(atob(padded));

        const firstName =
            payload.first_name ||
            payload.given_name ||
            (payload.name ? String(payload.name).split(' ')[0] : null);

        return firstName || (fallbackEmail ? fallbackEmail.split('@')[0] : '');
    } catch (err) {
        console.warn('Could not decode JWT for display name', err);
        return fallbackEmail || '';
    }
}

// Put user name + email in the sidebar footer
const sidebarUserNameSpan = document.getElementById("sidebar-user-name");
if (sidebarUserNameSpan) {
    sidebarUserNameSpan.textContent = getDisplayNameFromToken(authToken, authEmail);
}
const sidebarUserEmailSpan = document.getElementById("sidebar-user-email");
if (sidebarUserEmailSpan && authEmail) {
    sidebarUserEmailSpan.textContent = authEmail;
}

// Set user avatar initials
const userAvatarEl = document.getElementById("user-avatar");
if (userAvatarEl) {
    const displayName = getDisplayNameFromToken(authToken, authEmail);
    const initials = displayName
        .split(/\s+/)
        .filter(Boolean)
        .map(n => n[0].toUpperCase())
        .slice(0, 2)
        .join('');
    userAvatarEl.textContent = initials || '?';
}

// Centralised forced-logout handler
function forceLogout(reason) {
    console.warn('Forcing logout:', reason);

    // Best-effort: tell Django to invalidate all tokens for this user
    try {
        const email =
            localStorage.getItem('fa_user_email') ||
            localStorage.getItem('fa_email');

        if (email) {
            fetch('/proxy/logout/', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email }),
            }).catch(() => {
                // Ignore network/logging errors here – logout must not break the UI
            });
        }
    } catch (e) {
        console.warn('Backend logout failed', e);
    }

    // Clear local tokens
    authToken    = null;
    refreshToken = null;

    localStorage.removeItem('fa_access_token');
    localStorage.removeItem('fa_refresh_token');
    localStorage.removeItem('fa_user_uuid');
    localStorage.removeItem('fa_user_email');
    localStorage.removeItem('fa_email');

    // Reset UI bits if present
    const chatEl = document.getElementById('chat');
    if (chatEl) chatEl.innerHTML = '';

    const sessList = document.getElementById('session-list');
    if (sessList) sessList.innerHTML = '';

    // Go back to login
    window.location.href = "/";
}

// Logout button uses forceLogout
const logoutBtn = document.getElementById("logout-btn");
if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
        forceLogout('user_clicked_logout');
    });
}

// Helper to build headers for Django backend calls
function backendHeaders() {
    const h = { 'Content-Type': 'application/json' };

    // Send the current access token for normal auth
    if (authToken) {
        h['Authorization'] = 'Bearer ' + authToken;
    }

    // Send refresh token so Django can transparently refresh if needed
    if (refreshToken) {
        h['X-Refresh-Token'] = refreshToken;
    }

    return h;
}

// If Django returns a refreshed access_token (from validate_authorization_header),
// update localStorage + in-memory variables.
function maybeUpdateTokenFromResponse(data) {
    if (!data || typeof data !== 'object') return;

    if (data.access_token) {
        authToken = data.access_token;
        localStorage.setItem('fa_access_token', authToken);
    }

    // Optional: if backend also returns a new refresh token in future
    if (data.refresh_token) {
        refreshToken = data.refresh_token;
        localStorage.setItem('fa_refresh_token', refreshToken);
    }
}

// Show the app layout (remove the "hidden" class)
const appLayout = document.getElementById('app-layout');
if (appLayout) {
    appLayout.classList.remove('hidden');
}


// -----------------------------
// Sessions + logging
// -----------------------------

// Current ChatSession.session_uuid (string or null)
let currentSessionUuid = null;
let currentSessionTitle = '';
let currentSessionMessageCount = 0;
let activeDocIds = [];
let lastSentAttachments = [];
const UNTITLED_LABEL = '(untitled)';
const TEMP_TITLE_LABEL = 'Thinking...';
const sessionTitleByUuid = new Map();
let floatingSessionMenu = null;
let floatingSessionMenuState = { sessionUuid: null, title: '' };
let floatingSessionMenuHandlersBound = false;
// Last user prompt (for logging)
let lastUserQuestion = '';
let pendingTitleSessionUuid = null;

function getChatIdFromUrl() {
    try {
        const url = new URL(window.location.href);
        const pathMatch = url.pathname.match(/^\/c\/([^/]+)$/);
        if (pathMatch && pathMatch[1]) return pathMatch[1].trim();
        return (url.searchParams.get('chat') || '').trim();
    } catch {
        return '';
    }
}

function syncChatUrl(sessionUuid) {
    try {
        const url = new URL(window.location.href);
        url.searchParams.delete('chat');
        url.pathname = sessionUuid ? `/c/${sessionUuid}` : '/chat';
        window.history.replaceState({ chatId: sessionUuid || null }, '', url.toString());
    } catch {}
}

function readPendingChatStore() {
    try {
        const raw = sessionStorage.getItem(PENDING_CHAT_MESSAGES_KEY);
        const parsed = raw ? JSON.parse(raw) : {};
        return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
        return {};
    }
}

function writePendingChatStore(store) {
    try {
        sessionStorage.setItem(PENDING_CHAT_MESSAGES_KEY, JSON.stringify(store || {}));
    } catch {}
}

function readMessageFeedbackStore() {
    try {
        const raw = localStorage.getItem(MESSAGE_FEEDBACK_KEY);
        const parsed = raw ? JSON.parse(raw) : {};
        return parsed && typeof parsed === 'object' ? parsed : {};
    } catch {
        return {};
    }
}

function writeMessageFeedbackStore(store) {
    try {
        localStorage.setItem(MESSAGE_FEEDBACK_KEY, JSON.stringify(store || {}));
    } catch {}
}

function messageFeedbackKey(messageText) {
    return `${currentSessionUuid || 'no-session'}::${String(messageText || '').slice(0, 500)}`;
}

function extractMessageId(payload) {
    if (!payload || typeof payload !== 'object') return '';
    return String(
        payload.message_id ||
        payload.assistant_message_id ||
        payload.id ||
        payload.message?.id ||
        payload.message?.message_id ||
        ''
    ).trim();
}

function getSvgIcon(name) {
    const icons = {
        upload: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V5M7.5 9.5 12 5l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M5 19h14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
        send: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M21 3 10 14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="m21 3-7 18-4-7-7-4 18-7Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
        stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
        copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 9a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-8a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M5 15H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v1" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
        like: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 21H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3zm0-9 4-9c.4-1 2-1 2 0v4h5.6c1.3 0 2.2 1.2 1.8 2.4l-1.9 7A2 2 0 0 1 16.6 18H7z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
        dislike: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M17 3h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3zm0 9-4 9c-.4 1-2 1-2 0v-4H5.4c-1.3 0-2.2-1.2-1.8-2.4l1.9-7A2 2 0 0 1 7.4 6H17z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
        share: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3h7v7" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M10 14 21 3" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
        retry: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0 2 5.3" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M20 4v7h-7" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
        edit: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4l10-10-4-4L4 16v4z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="m13 6 4 4" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
        more: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.8" fill="currentColor"/><circle cx="12" cy="12" r="1.8" fill="currentColor"/><circle cx="19" cy="12" r="1.8" fill="currentColor"/></svg>',
        speaker: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 9v6h4l5 4V5L9 9z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M18 9a4 4 0 0 1 0 6M20.5 6.5a7.5 7.5 0 0 1 0 11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    };
    return icons[name] || '';
}

async function sendMessageFeedback(sessionUuid, messageId, feedback, meta = {}) {
    if (!authToken || !sessionUuid || !messageId) return false;
    try {
        const res = await fetch(`${MESSAGE_FEEDBACK_BASE}/${encodeURIComponent(sessionUuid)}/message/${encodeURIComponent(messageId)}/feedback`, {
            method: 'POST',
            headers: backendHeaders(),
            body: JSON.stringify({ feedback, meta }),
        });
        const data = await res.json().catch(() => ({}));
        maybeUpdateTokenFromResponse(data);
        if (!res.ok) {
            if (handleAuthFailure(res, data, 'messageFeedback')) return false;
            return false;
        }
        return !data.status || data.status === 'success';
    } catch {
        return false;
    }
}

function getPendingMessages(sessionUuid) {
    if (!sessionUuid) return [];
    const store = readPendingChatStore();
    const msgs = store[sessionUuid];
    return Array.isArray(msgs) ? msgs : [];
}

function setPendingMessages(sessionUuid, messages) {
    if (!sessionUuid) return;
    const store = readPendingChatStore();
    if (messages && messages.length) store[sessionUuid] = messages;
    else delete store[sessionUuid];
    writePendingChatStore(store);
}

function appendPendingTurn(sessionUuid, userText, assistantText, latencyMs) {
    if (!sessionUuid || !userText || !assistantText) return;
    const existing = getPendingMessages(sessionUuid);
    existing.push(
        { role: 'user', content: userText },
        { role: 'assistant', content: assistantText, latency_ms: latencyMs }
    );
    setPendingMessages(sessionUuid, existing.slice(-20));
}

function movePendingMessages(fromSessionUuid, toSessionUuid) {
    if (!fromSessionUuid || !toSessionUuid || fromSessionUuid === toSessionUuid) return;
    const fromMessages = getPendingMessages(fromSessionUuid);
    if (!fromMessages.length) return;
    const merged = mergeMessagesForDisplay(getPendingMessages(toSessionUuid), fromMessages);
    setPendingMessages(toSessionUuid, merged.slice(-20));
    setPendingMessages(fromSessionUuid, []);
}

function mergeMessagesForDisplay(baseMessages, extraMessages) {
    const merged = (Array.isArray(baseMessages) ? baseMessages : []).filter(Boolean).map(m => ({ ...m }));
    const extras = (Array.isArray(extraMessages) ? extraMessages : []).filter(Boolean);
    if (!extras.length) return merged;

    const existingPairs = merged
        .filter(m => (m.content || '').trim())
        .map(m => [String(m.role || '').trim().toLowerCase(), String(m.content || '').trim()]);

    let scanStart = 0;
    extras.forEach((msg) => {
        const role = String(msg.role || '').trim().toLowerCase();
        const content = String(msg.content || '').trim();
        if (!content) return;

        let foundAt = -1;
        for (let idx = scanStart; idx < existingPairs.length; idx += 1) {
            const [existingRole, existingContent] = existingPairs[idx];
            if (existingRole === role && existingContent === content) {
                foundAt = idx;
                break;
            }
        }

        if (foundAt >= 0) {
            scanStart = foundAt + 1;
            return;
        }

        merged.push({ ...msg, role });
        existingPairs.push([role, content]);
        scanStart = existingPairs.length;
    });

    return merged;
}

function prunePersistedPendingMessages(sessionUuid, backendMessages) {
    const pending = getPendingMessages(sessionUuid);
    if (!pending.length) return;

    const remaining = [];
    const backendPairs = (Array.isArray(backendMessages) ? backendMessages : [])
        .filter(m => (m.content || '').trim())
        .map(m => [String(m.role || '').trim().toLowerCase(), String(m.content || '').trim()]);

    let scanStart = 0;
    pending.forEach((msg) => {
        const role = String(msg.role || '').trim().toLowerCase();
        const content = String(msg.content || '').trim();
        if (!content) return;

        let foundAt = -1;
        for (let idx = scanStart; idx < backendPairs.length; idx += 1) {
            const [existingRole, existingContent] = backendPairs[idx];
            if (existingRole === role && existingContent === content) {
                foundAt = idx;
                break;
            }
        }

        if (foundAt >= 0) {
            scanStart = foundAt + 1;
            return;
        }

        remaining.push(msg);
    });

    setPendingMessages(sessionUuid, remaining);
}

function shortTitleFromText(text) {
    const cleaned = String(text || '')
        .replace(/[^\w\s-]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    if (!cleaned) return 'New chat';

    const stopWords = new Set([
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'to', 'for', 'of', 'in',
        'on', 'at', 'and', 'or', 'with', 'how', 'what', 'which', 'can', 'could',
        'would', 'should', 'do', 'does', 'did', 'please', 'tell', 'me', 'about',
        'my', 'our', 'your'
    ]);

    const tokens = cleaned
        .split(' ')
        .filter(t => t && !stopWords.has(t.toLowerCase()));
    const chosen = (tokens.length ? tokens : cleaned.split(' ')).slice(0, 3);
    return chosen.join(' ').slice(0, 120);
}

function parseSessionDate(session) {
    const raw = session?.updated_at || session?.created_at || session?.modified_at || '';
    const dt = raw ? new Date(raw) : null;
    return dt && !Number.isNaN(dt.getTime()) ? dt : null;
}

function getSessionGroupLabel(session) {
    const dt = parseSessionDate(session);
    if (!dt) return 'Older';

    const now = new Date();
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const startYesterday = new Date(startToday);
    startYesterday.setDate(startYesterday.getDate() - 1);
    const startLastWeek = new Date(startToday);
    startLastWeek.setDate(startLastWeek.getDate() - 7);
    const startLastMonth = new Date(startToday);
    startLastMonth.setMonth(startLastMonth.getMonth() - 1);

    if (dt >= startToday) return 'Today';
    if (dt >= startYesterday) return 'Yesterday';
    if (dt >= startLastWeek) return 'Last 7 Days';
    if (dt >= startLastMonth) return 'Last 30 Days';
    return 'Older';
}

// Shared helper: handle 401/400 auth failures from Django
function handleAuthFailure(res, data, contextLabel) {
    if (res.status === 401 || res.status === 400) {
        // Access token invalid AND refresh token invalid/absent
        forceLogout(
            `${contextLabel}_auth_failed: ` + (data && data.message ? data.message : '')
        );
        return true;
    }
    return false;
}

// Load all sessions for the sidebar
async function loadSessions() {
    if (!authToken) return;

    try {
        const res = await fetch(SESSIONS_URL, {
            method: 'GET',
            headers: backendHeaders(),
        });

        const data = await res.json().catch(() => ({}));
        maybeUpdateTokenFromResponse(data);

        if (!res.ok) {
             // 1) deal with auth failures (401 / 400) in a single place
            if (handleAuthFailure(res, data, 'loadSessions')) return;

             // 2) non-auth HTTP error
            console.warn('Failed to load sessions (HTTP)', res.status, data);
            return;
        }

        // 3) HTTP OK but API reported failure
        if (data.status !== 'success') {
            console.warn('Failed to load sessions (status)', data);
            return;
        }

        const sessions = data.sessions || [];
        if (pendingTitleSessionUuid) {
            const pendingSession = sessions.find(s => s.session_uuid === pendingTitleSessionUuid);
            if (pendingSession && pendingSession.title && pendingSession.title !== UNTITLED_LABEL) {
                pendingTitleSessionUuid = null;
            }
        }
        renderSessionList(sessions);
    } catch (err) {
        console.error('Error loading sessions', err);
    }
}

async function renameSession(sessionUuid, currentTitle = '') {
    if (!authToken || !sessionUuid) return false;

    const seed = (currentTitle || '').trim();
    const nextTitleRaw = window.prompt('Rename chat title:', seed);
    if (nextTitleRaw == null) return false; // user cancelled

    const nextTitle = nextTitleRaw.trim().slice(0, 120);
    if (!nextTitle) {
        window.alert('Title cannot be empty.');
        return false;
    }

    try {
        const res = await fetch(`${SESSIONS_URL}${sessionUuid}/`, {
            method: 'PATCH',
            headers: backendHeaders(),
            body: JSON.stringify({ title: nextTitle }),
        });

        const data = await res.json().catch(() => ({}));
        maybeUpdateTokenFromResponse(data);

        if (!res.ok) {
            if (handleAuthFailure(res, data, 'renameSession')) return false;
            console.warn('Failed to rename session (HTTP)', res.status, data);
            window.alert((data && data.message) ? data.message : `Rename failed (HTTP ${res.status})`);
            return false;
        }

        if (data.status && data.status !== 'success') {
            console.warn('Failed to rename session (status)', data);
            window.alert(data.message || 'Rename failed.');
            return false;
        }

        sessionTitleByUuid.set(sessionUuid, nextTitle);
        if (currentSessionUuid === sessionUuid) currentSessionTitle = nextTitle;
        loadSessions().catch(console.error);
        return true;
    } catch (err) {
        console.error('Error renaming session', err);
        window.alert('Rename request failed.');
        return false;
    }
}

async function deleteSession(sessionUuid) {
    if (!authToken || !sessionUuid) return false;

    try {
        const res = await fetch(`${SESSIONS_URL}${sessionUuid}/`, {
            method: 'DELETE',
            headers: backendHeaders(),
        });

        const data = await res.json().catch(() => ({}));
        maybeUpdateTokenFromResponse(data);

        if (!res.ok) {
            if (handleAuthFailure(res, data, 'deleteSession')) return false;
            console.warn('Failed to delete session (HTTP)', res.status, data);
            return false;
        }

        if (data.status !== 'success') {
            console.warn('Failed to delete session (status)', data);
            return false;
        }

        setPendingMessages(sessionUuid, []);

        if (currentSessionUuid === sessionUuid) {
            currentSessionUuid = null;
            currentSessionTitle = '';
            currentSessionMessageCount = 0;
            syncChatUrl(null);
            const chatEl = document.getElementById('chat');
            if (chatEl) chatEl.innerHTML = '';
        }

        loadSessions().catch(console.error);
        return true;
    } catch (err) {
        console.error('Error deleting session', err);
        return false;
    }
}

function closeSessionActionMenu() {
    if (!floatingSessionMenu) return;
    floatingSessionMenu.classList.add('hidden');
    floatingSessionMenuState = { sessionUuid: null, title: '' };
}

function ensureFloatingSessionMenu() {
    if (floatingSessionMenu) return floatingSessionMenu;

    const menu = document.createElement('div');
    menu.className = 'session-menu session-menu-portal hidden';

    const renameItem = document.createElement('button');
    renameItem.type = 'button';
    renameItem.className = 'session-menu-item';
    renameItem.textContent = 'Rename';
    renameItem.addEventListener('click', async (e) => {
        e.stopPropagation();
        const { sessionUuid, title } = floatingSessionMenuState;
        closeSessionActionMenu();
        if (!sessionUuid) return;
        await renameSession(sessionUuid, title);
    });

    const deleteItem = document.createElement('button');
    deleteItem.type = 'button';
    deleteItem.className = 'session-menu-item danger';
    deleteItem.textContent = 'Delete';
    deleteItem.addEventListener('click', async (e) => {
        e.stopPropagation();
        const { sessionUuid } = floatingSessionMenuState;
        closeSessionActionMenu();
        if (!sessionUuid) return;
        const ok = window.confirm('Delete this chat?');
        if (!ok) return;
        await deleteSession(sessionUuid);
    });

    menu.appendChild(renameItem);
    menu.appendChild(deleteItem);
    menu.addEventListener('click', (e) => e.stopPropagation());

    document.body.appendChild(menu);
    floatingSessionMenu = menu;

    if (!floatingSessionMenuHandlersBound) {
        document.addEventListener('click', () => closeSessionActionMenu());
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeSessionActionMenu();
        });
        window.addEventListener('resize', () => closeSessionActionMenu());
        window.addEventListener('scroll', () => closeSessionActionMenu(), true);
        floatingSessionMenuHandlersBound = true;
    }

    return menu;
}

function openSessionActionMenu(triggerBtn, sessionUuid, title) {
    const menu = ensureFloatingSessionMenu();
    const isOpeningSame =
        !menu.classList.contains('hidden') && floatingSessionMenuState.sessionUuid === sessionUuid;
    if (isOpeningSame) {
        closeSessionActionMenu();
        return;
    }

    floatingSessionMenuState = { sessionUuid, title };
    menu.classList.remove('hidden');

    const btnRect = triggerBtn.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const gap = 6;

    let left = btnRect.right - menuRect.width;
    if (left < 8) left = 8;
    if (left + menuRect.width > window.innerWidth - 8) {
        left = window.innerWidth - menuRect.width - 8;
    }

    let top = btnRect.bottom + gap;
    if (top + menuRect.height > window.innerHeight - 8) {
        top = btnRect.top - menuRect.height - gap;
    }
    if (top < 8) top = 8;

    menu.style.left = `${left}px`;
    menu.style.top = `${top}px`;
}

function renderSessionList(sessions) {
    const listEl = document.getElementById('session-list');
    if (!listEl) return;
    listEl.innerHTML = '';
    sessionTitleByUuid.clear();
    ensureFloatingSessionMenu();

    const orderedGroups = ['Today', 'Yesterday', 'Last 7 Days', 'Last 30 Days', 'Older'];
    const groupedSessions = new Map(orderedGroups.map(label => [label, []]));

    sessions.forEach(sess => {
        const groupLabel = getSessionGroupLabel(sess);
        if (!groupedSessions.has(groupLabel)) groupedSessions.set(groupLabel, []);
        groupedSessions.get(groupLabel).push(sess);
    });

    orderedGroups.forEach((groupLabel) => {
        const groupItems = groupedSessions.get(groupLabel) || [];
        if (!groupItems.length) return;

        const heading = document.createElement('li');
        heading.className = 'session-group-label';
        heading.textContent = groupLabel;
        listEl.appendChild(heading);

        groupItems.forEach(sess => {
            const isPendingTitle = pendingTitleSessionUuid === sess.session_uuid && (!sess.title || sess.title === UNTITLED_LABEL);
            const safeTitle = isPendingTitle ? TEMP_TITLE_LABEL : (sess.title || UNTITLED_LABEL);
            sessionTitleByUuid.set(sess.session_uuid, safeTitle);

            const li = document.createElement('li');
            li.className = 'session-item';
            li.dataset.uuid = sess.session_uuid;

            const titleSpan = document.createElement('span');
            titleSpan.className = 'session-title';
            if (isPendingTitle) titleSpan.classList.add('pending-title', 'blink');
            titleSpan.textContent = safeTitle;
            titleSpan.addEventListener('click', () => {
                closeSessionActionMenu();
                openSession(sess.session_uuid);
            });

            const actionsWrap = document.createElement('div');
            actionsWrap.className = 'session-actions';

            const menuBtn = document.createElement('button');
            menuBtn.type = 'button';
            menuBtn.className = 'session-menu-trigger';
            menuBtn.title = 'Chat options';
            menuBtn.textContent = '⋯';

            menuBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                openSessionActionMenu(menuBtn, sess.session_uuid, safeTitle);
            });
            actionsWrap.appendChild(menuBtn);

            li.appendChild(titleSpan);
            li.appendChild(actionsWrap);
            listEl.appendChild(li);
        });
    });
}

// Create a new session (called on first question or when user presses "+ New")
async function createNewSession(initialTitle = '') {
    if (!authToken) return null;

    try {
        const res = await fetch(SESSIONS_URL, {
            method: 'POST',
            headers: backendHeaders(),
            body: JSON.stringify({
                title: initialTitle.slice(0, 120),
                metadata: { created_from: 'farm-assistant-ui' },
            }),
        });

        const data = await res.json().catch(() => ({}));
        maybeUpdateTokenFromResponse(data);

        if (!res.ok) {
            if (handleAuthFailure(res, data, 'createNewSession')) return null;
            console.warn('Failed to create session (HTTP)', res.status, data);
            return null;
        }

        if (data.status !== 'success') {
            console.warn('Failed to create session (status)', data);
            return null;
        }

        currentSessionUuid = data.session_uuid;
        currentSessionTitle = (data.title || initialTitle || '').trim();
        currentSessionMessageCount = 0;
        pendingTitleSessionUuid = currentSessionUuid;
        syncChatUrl(currentSessionUuid);
        // Reload list so the new chat appears at the top
        loadSessions().catch(console.error);
        return currentSessionUuid;
    } catch (err) {
        console.error('Error creating session', err);
        return null;
    }
}

// Ensure a session exists before logging the first turn
async function ensureSessionExists(firstUserText) {
    if (currentSessionUuid) return currentSessionUuid;
    return await createNewSession('');
}

// Load one session and render its messages
async function openSession(sessionUuid) {
    if (!authToken) return;
    currentSessionUuid = sessionUuid;
    syncChatUrl(sessionUuid);
    activeDocIds = [];
    clearUploadedFileChips();

    try {
        const res = await fetch(`${SESSIONS_URL}${sessionUuid}/`, {
            method: 'GET',
            headers: backendHeaders(),
        });

        const data = await res.json().catch(() => ({}));
        maybeUpdateTokenFromResponse(data);

        if (!res.ok) {
            if (handleAuthFailure(res, data, 'openSession')) return;
            console.warn('Failed to load session (HTTP)', res.status, data);
            return;
        }

        // Be tolerant: some backends may omit `status` but still return `messages`.
        if (data.status && data.status !== 'success') {
            console.warn('Failed to load session (status)', data);
            return;
        }

        const sessionObj = data.session || {};
        const backendMessages = Array.isArray(data.messages) ? data.messages : [];
        const pendingMessages = getPendingMessages(sessionUuid);
        const messages = mergeMessagesForDisplay(backendMessages, pendingMessages);
        prunePersistedPendingMessages(sessionUuid, backendMessages);
        currentSessionTitle = (sessionObj.title || sessionTitleByUuid.get(sessionUuid) || '').trim();
        currentSessionMessageCount = messages.length;

        const chatEl = document.getElementById('chat');
        if (!chatEl) return;
        chatEl.innerHTML = '';

        messages.forEach(m => {
            const role = m.role === 'user' ? 'you' : 'assistant';
            const bubble = addMessage(role, m.content);
            if (role === 'assistant') {
                const messageId = extractMessageId(m);
                if (messageId) bubble.dataset.messageId = messageId;
                renderAssistantRichText(bubble);
                attachAssistantFooter(bubble, m.latency_ms, 'Completed.');
                attachResponseActions(bubble);
                const q = extractLastQuestion(m.content || '');
                if (q) lastAssistantFollowupQuestion = q;
            }
        });

        window.requestAnimationFrame(() => {
            scrollToBottom();
        });

        if (!messages.length) {
            console.info('Session has no messages or backend returned empty list', {
                session_uuid: sessionUuid,
                payload_keys: Object.keys(data || {}),
            });
        }
    } catch (err) {
        console.error('Error loading session detail', err);
    }
}

// "+ New" button
const newChatBtn = document.getElementById('new-chat');
if (newChatBtn) {
    newChatBtn.addEventListener('click', async () => {
        const chatEl = document.getElementById('chat');
        if (chatEl) chatEl.innerHTML = '';
        // Start a fresh local thread; first turn will create the backend session.
        currentSessionUuid = null;
        currentSessionTitle = '';
        currentSessionMessageCount = 0;
        syncChatUrl(null);
        activeDocIds = [];
        clearUploadedFileChips();
        const inputEl = document.getElementById('question');
        if (inputEl) inputEl.focus();
    });
}

// Log each completed Q&A turn to Django
async function logChatTurnToBackend(userText, assistantText, latencyMs) {
    debugLog('Logging chat turn:', { userText: userText?.substring(0, 50), assistantText: assistantText?.substring(0, 50), session: currentSessionUuid });
    
    if (!authToken) {
        console.warn('Cannot log: no auth token');
        return;
    }
    if (!userText || !assistantText) {
        console.warn('Cannot log: missing text', { userText, assistantText });
        return;
    }

    if (currentSessionUuid) {
        appendPendingTurn(currentSessionUuid, userText, assistantText, latencyMs);
    }

    debugLog('Sending to:', LOG_TURN_URL);
    
    try {
        const res = await fetch(LOG_TURN_URL, {
            method: 'POST',
            headers: backendHeaders(),
            body: JSON.stringify({
                session_uuid: currentSessionUuid,   // may be null; backend will create
                user_message: userText,
                assistant_message: assistantText,
                meta: {
                    model: selModel ? selModel.value : null,
                    latency_ms: latencyMs,
                    attachments: lastSentAttachments,
                    ...(nextTurnMeta || {}),
                },
            }),
        });

        const data = await res.json().catch(() => ({}));
        maybeUpdateTokenFromResponse(data);

        if (!res.ok) {
            if (handleAuthFailure(res, data, 'logChatTurn')) return;
            console.warn('Failed to log chat turn (HTTP)', res.status, data);
            return;
        }

        if (data.status !== 'success') {
            console.warn('Failed to log chat turn (status)', data);
            return;
        }

        // Backend is authoritative for session UUID.
        // If backend returns a different UUID (e.g., it created/recovered another session),
        // sync local state so sidebar/session-open use the correct conversation.
        if (data.session_uuid && data.session_uuid !== currentSessionUuid) {
            console.info('Switching to backend session_uuid returned by log-turn', {
                previous_session_uuid: currentSessionUuid,
                backend_session_uuid: data.session_uuid,
            });
            movePendingMessages(currentSessionUuid, data.session_uuid);
            currentSessionUuid = data.session_uuid;
            syncChatUrl(currentSessionUuid);
            loadSessions().catch(console.error);
        } else if (!currentSessionUuid && data.session_uuid) {
            currentSessionUuid = data.session_uuid;
            syncChatUrl(currentSessionUuid);
            loadSessions().catch(console.error);
        }

        if (currentSessionUuid) {
            prunePersistedPendingMessages(currentSessionUuid, [
                { role: 'user', content: userText },
                { role: 'assistant', content: assistantText },
            ]);
        }

        const assistantMessageId = extractMessageId(data);
        if (assistantMessageId && answerNode) {
            answerNode.dataset.messageId = assistantMessageId;
            attachResponseActions(answerNode);
        }

        if (!currentSessionTitle || currentSessionTitle === UNTITLED_LABEL) {
            currentSessionTitle = shortTitleFromText(userText);
        }
        currentSessionMessageCount += 2;
        nextTurnMeta = null;
        if (currentSessionUuid && pendingTitleSessionUuid === currentSessionUuid) {
            window.setTimeout(() => loadSessions().catch(console.error), 1200);
            window.setTimeout(() => loadSessions().catch(console.error), 3000);
        }
    } catch (err) {
        console.error('Error logging chat turn', err);
        nextTurnMeta = null;
    }
}

async function maybeReseedUntitledEmptySession(firstUserText) {
    if (!currentSessionUuid) return;
    const t = (currentSessionTitle || '').trim().toLowerCase();
    const isUntitled = !t || t === UNTITLED_LABEL;
    if (!isUntitled || currentSessionMessageCount > 0) return;

    const oldSessionUuid = currentSessionUuid;
    const replacement = await createNewSession(shortTitleFromText(firstUserText));
    if (replacement) {
        console.info('Switched from empty untitled session', {
            old_session_uuid: oldSessionUuid,
            new_session_uuid: replacement,
        });
    }
}

// -----------------------------
// Core chat streaming logic
// -----------------------------

const chat = document.getElementById('chat');
const form = document.getElementById('qform');
const input = document.getElementById('question');
const btnSend = document.getElementById('send');
const selModel = document.getElementById('model');
const uploadPdfBtn = document.getElementById('upload-pdf');
const pdfFileInput = document.getElementById('pdf-file');
const uploadedFilesEl = document.getElementById('uploaded-files');

if (uploadPdfBtn) {
    uploadPdfBtn.innerHTML = `${getSvgIcon('upload')}<span class="sr-only">Upload PDF</span>`;
}

function clearUploadedFileChips() {
    if (!uploadedFilesEl) return;
    uploadedFilesEl.innerHTML = '';
    uploadedFilesEl.classList.add('hidden');
}

// Auto-resize textarea
function autoResizeTextarea() {
    if (!input) return;
    input.style.height = 'auto';
    const newHeight = Math.min(input.scrollHeight, 200); // max-height: 200px
    input.style.height = newHeight + 'px';
}

function hasComposerDraft() {
    return !!(input && input.value.trim());
}

if (input) {
    input.addEventListener('input', () => {
        autoResizeTextarea();
        updateComposerPrimaryButton(!!es);
    });
}

let es = null;
let answerNode = null;
let statusNode = null;
let cancelled = false;
let pendingSources = [];

let clientStartTs = 0;
let serverTimingMs = null;
let statusLeft = null;
let statusRight = null;
let lastAssistantFollowupQuestion = '';
let nextTurnMeta = null;
let pendingGroundingMode = '';

function updateComposerPrimaryButton(isStreaming) {
    if (!btnSend) return;
    btnSend.classList.toggle('is-stop', !!isStreaming);
    btnSend.disabled = !isStreaming && !hasComposerDraft();
    btnSend.title = isStreaming ? 'Stop response' : 'Send message';
    btnSend.setAttribute('aria-label', isStreaming ? 'Stop response' : 'Send message');
    btnSend.innerHTML = isStreaming
        ? `${getSvgIcon('stop')}<span class="sr-only">Stop</span>`
        : `${getSvgIcon('send')}<span class="sr-only">Send</span>`;
}

function isShortAffirmation(text) {
    const t = String(text || '')
        .trim()
        .toLowerCase()
        .replace(/[^\w\s]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    if (!t) return false;
    const tokens = t.split(/\s+/).filter(Boolean);
    if (tokens.length > 4) return false;
    const canonical = new Set([
        'yes', 'yea', 'yeah', 'yep', 'yup',
        'yes please', 'please', 'sure', 'sure thing',
        'okay', 'ok', 'alright', 'all right',
        'go ahead', 'continue', 'more', 'tell me more', 'sounds good',
    ]);
    return canonical.has(t);
}

function extractLastQuestion(text) {
    const raw = String(text || '').trim();
    if (!raw || !raw.includes('?')) return '';
    const parts = raw.split(/(?<=[\?\!\.])\s+/).map(s => s.trim()).filter(Boolean);
    for (let i = parts.length - 1; i >= 0; i -= 1) {
        if (parts[i].endsWith('?')) return parts[i].slice(0, 300);
    }
    return '';
}

function formatDuration(ms) {
    if (ms == null || !isFinite(ms)) return '';
    if (ms < 1000) return '<1s';
    const secs = Math.floor(ms / 1000);
    const s = secs % 60;
    const m = Math.floor(secs / 60) % 60;
    const h = Math.floor(secs / 3600);
    if (h > 0 && m > 0 && s > 0) return `${h}h ${m}m ${s}s`;
    if (h > 0 && m > 0) return `${h}h ${m}m`;
    if (h > 0) return `${h}h`;
    if (m > 0 && s > 0) return `${m}m ${s}s`;
    if (m > 0) return `${m}m`;
    return `${s}s`;
}

function insertThinkTime(container, ms) {
    const line = document.createElement('div');
    line.className = 'muted';
    line.style.marginTop = '10px';
    line.style.marginBottom = '6px';

    container.parentElement.appendChild(line);
    return line;
}

function scrollToBottom() {
    chat.scrollTop = chat.scrollHeight;
}

function getStatusPartsForBubble(bubble) {
    if (!bubble || !bubble.parentElement) return { row: null, left: null, right: null };
    const container = bubble.parentElement;
    let row = container.querySelector('.status');

    if (!row) {
        row = document.createElement('div');
        row.className = 'status';

        const left = document.createElement('span');
        left.className = 'left';

        const right = document.createElement('span');
        right.className = 'right';

        row.appendChild(left);
        row.appendChild(right);
        container.appendChild(row);
    }

    return {
        row,
        left: row.querySelector('.left'),
        right: row.querySelector('.right'),
    };
}

function speakBubbleText(bubble) {
    if (!bubble || !window.speechSynthesis) return;
    const text = bubble.dataset.rawText || bubble.textContent || '';
    if (!text.trim()) return;

    const utter = new SpeechSynthesisUtterance(text);
    const voices = window.speechSynthesis.getVoices() || [];
    const gbVoices = voices.filter(v =>
        v.lang && v.lang.toLowerCase().startsWith('en-gb')
    );
    if (gbVoices.length) {
        utter.voice = gbVoices[0];
    }

    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(utter);
}

async function copyTextToClipboard(text) {
    if (!text) return false;
    try {
        await navigator.clipboard.writeText(text);
        return true;
    } catch {
        return false;
    }
}

function getAssociatedUserText(container) {
    let cursor = container ? container.previousElementSibling : null;
    while (cursor) {
        if (cursor.classList && cursor.classList.contains('msg') && cursor.classList.contains('you')) {
            const bubble = cursor.querySelector('.bubble');
            return (bubble?.textContent || '').trim();
        }
        cursor = cursor.previousElementSibling;
    }
    return '';
}

function attachResponseActions(bubble) {
    if (!bubble || !bubble.parentElement) return;
    const container = bubble.parentElement;

    const existing = container.querySelector('.response-actions');
    if (existing) existing.remove();

    const actions = document.createElement('div');
    actions.className = 'response-actions';
    const copyNotice = document.createElement('span');
    copyNotice.className = 'response-action-notice hidden';
    let copyNoticeTimer = null;

    const rawText = bubble.dataset.rawText || bubble.textContent || '';
    const feedbackStore = readMessageFeedbackStore();
    const feedbackKey = messageFeedbackKey(rawText);
    let feedbackValue = feedbackStore[feedbackKey] || '';

    function makeIconButton(label, title, onClick) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'response-action-btn';
        btn.innerHTML = `${getSvgIcon(label)}<span class="sr-only">${title}</span>`;
        btn.title = title;
        btn.addEventListener('click', onClick);
        return btn;
    }

    function applyFeedbackState(likeBtn) {
        likeBtn.classList.toggle('active', feedbackValue === 'up');
    }

    function showActionNotice(text) {
        copyNotice.textContent = text;
        copyNotice.classList.remove('hidden');
        if (copyNoticeTimer) window.clearTimeout(copyNoticeTimer);
        copyNoticeTimer = window.setTimeout(() => {
            copyNotice.classList.add('hidden');
        }, 1500);
    }

    const copyBtn = makeIconButton('copy', 'Copy response', async () => {
        const ok = await copyTextToClipboard(rawText);
        copyBtn.classList.toggle('active', ok);
        setTimeout(() => copyBtn.classList.remove('active'), 1200);
        if (ok) showActionNotice('Copied');
    });

    const likeBtn = makeIconButton('like', 'Helpful', async () => {
        const nextValue = feedbackValue === 'up' ? '' : 'up';
        const ok = await sendMessageFeedback(currentSessionUuid, bubble.dataset.messageId || '', nextValue || 'none', {
            action: 'like',
        });
        if (!bubble.dataset.messageId || ok) {
            feedbackValue = nextValue;
        }
        const store = readMessageFeedbackStore();
        if (feedbackValue) store[feedbackKey] = feedbackValue;
        else delete store[feedbackKey];
        writeMessageFeedbackStore(store);
        applyFeedbackState(likeBtn);
    });

    const regenerateBtn = makeIconButton('retry', 'Regenerate response', async () => {
        const userText = getAssociatedUserText(container);
        if (!userText || es) return;
        regenerateBtn.disabled = true;
        try {
            // Drop this assistant turn and anything after it before re-running,
            // so the user sees a clean replacement, not an appended duplicate.
            removeMessagesFrom(container, { inclusive: true });

            await ensureSessionExists(userText);
            lastUserQuestion = userText;
            nextTurnMeta = {
                action: 'retry',
                retry_of_message_id: bubble.dataset.messageId || null,
            };
            startStream(userText, { skipUserEcho: true, replaceHistory: true });
        } finally {
            regenerateBtn.disabled = false;
        }
    });

    const readAloudBtn = makeIconButton('speaker', 'Read aloud', () => {
        speakBubbleText(bubble);
    });

    actions.appendChild(copyBtn);
    actions.appendChild(likeBtn);
    actions.appendChild(regenerateBtn);
    actions.appendChild(readAloudBtn);
    actions.appendChild(copyNotice);
    applyFeedbackState(likeBtn);

    container.appendChild(actions);
}

function attachAssistantFooter(bubble, latencyMs, stateText = 'Completed.') {
    if (!bubble) return;
    const { row, left, right } = getStatusPartsForBubble(bubble);
    if (!row || !left || !right) return;

    row.classList.remove('blink');
    left.textContent = stateText;
    right.textContent = (latencyMs != null && isFinite(latencyMs))
        ? `Thought for ${formatDuration(latencyMs)}`
        : '';
}

function renderGroundingBadge(bubble, mode) {
    if (!bubble || !bubble.parentElement || !mode) return;
    const container = bubble.parentElement;
    const existing = container.querySelector('.grounding-badge');
    if (existing) existing.remove();

    const badge = document.createElement('div');
    badge.className = `grounding-badge grounding-${mode}`;

    if (mode === 'euf_supported') {
        badge.textContent = 'EUF-supported';
    } else if (mode === 'general_fallback') {
        badge.textContent = 'General guidance';
    } else if (mode === 'general_knowledge') {
        badge.textContent = 'General knowledge';
    } else if (mode === 'history_only') {
        badge.textContent = 'Conversation recap';
    } else if (mode === 'conversation_only') {
        badge.textContent = 'Conversation-based';
    } else if (mode === 'assistant_capabilities') {
        badge.textContent = 'Capability answer';
    } else if (mode === 'off_topic') {
        badge.textContent = 'Off-topic';
    } else {
        return;
    }

    container.appendChild(badge);
}

function addMessage(role, text) {
    const box = document.createElement('div');
    box.className = `msg ${role}`;
    const who = document.createElement('div');
    who.className = 'role';
    who.textContent = role === 'you' ? 'You' : 'Assistant';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text || '';
    box.appendChild(who);
    box.appendChild(bubble);
    chat.appendChild(box);
    scrollToBottom();

    if (role === 'you') {
        bubble.dataset.userText = text || '';
        attachUserMessageActions(bubble);
    }
    return bubble;
}

function removeMessagesFrom(node, { inclusive }) {
    if (!node) return;
    let cursor = inclusive ? node : node.nextElementSibling;
    while (cursor) {
        const next = cursor.nextElementSibling;
        cursor.remove();
        cursor = next;
    }
}

function attachUserMessageActions(bubble) {
    if (!bubble || !bubble.parentElement) return;
    const container = bubble.parentElement;
    if (container.querySelector('.user-actions')) return;
    if (bubble.dataset.userText == null) {
        bubble.dataset.userText = bubble.textContent || '';
    }

    const actions = document.createElement('div');
    actions.className = 'user-actions';

    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'response-action-btn';
    editBtn.title = 'Edit and resend';
    editBtn.innerHTML = `${getSvgIcon('edit')}<span class="sr-only">Edit and resend</span>`;
    editBtn.addEventListener('click', () => beginEditUserMessage(container, bubble));

    actions.appendChild(editBtn);
    container.appendChild(actions);
}

function beginEditUserMessage(container, bubble) {
    if (es) return; // a stream is in progress; ignore until it ends
    if (container.querySelector('.user-edit-editor')) return;

    const original = bubble.dataset.userText || bubble.textContent || '';

    const editor = document.createElement('div');
    editor.className = 'user-edit-editor';

    const ta = document.createElement('textarea');
    ta.className = 'user-edit-textarea';
    ta.value = original;
    const lines = original.split('\n').length;
    ta.rows = Math.min(8, Math.max(2, lines + 1));

    const btnRow = document.createElement('div');
    btnRow.className = 'user-edit-actions';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn-cancel';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => {
        editor.remove();
        bubble.style.display = '';
    });

    const sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'btn-save';
    sendBtn.textContent = 'Send';
    const submitEdit = async () => {
        const newText = (ta.value || '').trim();
        if (!newText || es) return;
        sendBtn.disabled = true;
        try {
            bubble.textContent = newText;
            bubble.dataset.userText = newText;
            editor.remove();
            bubble.style.display = '';

            // Drop everything after this user message — assistant turn(s) are now stale.
            removeMessagesFrom(container, { inclusive: false });

            await ensureSessionExists(newText);
            lastUserQuestion = newText;
            nextTurnMeta = {
                action: 'edit',
                edited_from_message_id: bubble.dataset.messageId || null,
            };
            startStream(newText, { skipUserEcho: true, replaceHistory: true });
        } finally {
            sendBtn.disabled = false;
        }
    };
    sendBtn.addEventListener('click', submitEdit);
    ta.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            submitEdit();
        } else if (e.key === 'Escape') {
            e.preventDefault();
            editor.remove();
            bubble.style.display = '';
        }
    });

    btnRow.appendChild(cancelBtn);
    btnRow.appendChild(sendBtn);
    editor.appendChild(ta);
    editor.appendChild(btnRow);

    bubble.style.display = 'none';
    container.insertBefore(editor, bubble.nextSibling);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
}

function setStatus(text, thinking = false) {
    if (!statusNode) {
        statusNode = document.createElement('div');
        statusNode.className = 'status';

        statusLeft = document.createElement('span');
        statusLeft.className = 'left';

        statusRight = document.createElement('span');
        statusRight.className = 'right';

        statusNode.appendChild(statusLeft);
        statusNode.appendChild(statusRight);
        answerNode.parentElement.appendChild(statusNode);
    }

    statusLeft.textContent = text;

    if (thinking) statusNode.classList.add('blink');
    else statusNode.classList.remove('blink');
}

function renderSourcesInline(container, items) {
    if (!items || !items.length) return;

    if (DEBUG) {
        console.log('Rendering sources:', JSON.stringify(items, null, 2));
    }

    const ul = document.createElement('ul');
    ul.className = 'sources-list';

    items.forEach(s => {
        const li = document.createElement('li');

        const a = document.createElement('a');
        const href = s.url || s.display_url || '#';
        a.href = href;
        if (href !== '#') {
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
        }

        // Build label: prefer title, then project, then derive from URL, then clean up id
        // Note: sid (e.g. "S1") is intentionally NOT used as a label - it's just a reference marker
        let label = '';

        // 1) Use title if it's a meaningful non-empty string
        if (s.title && s.title.trim() && s.title.trim() !== s.id) {
            label = s.title.trim();
        }
        // 2) Use project name (e.g. "NETPOULSAFE (Horizon 2020)")
        else if (s.project && s.project.trim()) {
            label = s.project.trim();
        }
        // 3) Derive from URL domain
        else if (s.url || s.display_url) {
            try {
                const domain = new URL(href).hostname.replace(/^www\./, '');
                label = domain;
            } catch {
                // fall through
            }
        }

        // 4) Fall back to a cleaned-up id or sid
        if (!label) {
            // If we have a sid (like "S1"), use it with a descriptive fallback
            if (s.sid) {
                label = 'Source ' + s.sid;
            } else if (s.id) {
                // Extract last meaningful segment from S3-like keys
                // e.g. "bucket/path/to/doc-name.pdf" -> "doc-name.pdf"
                const idStr = String(s.id);
                const segments = idStr.split(/[\/\\]/);
                const lastSegment = segments[segments.length - 1];
                if (lastSegment) {
                    // Remove file extension for cleaner display
                    label = lastSegment.replace(/\.[^.]+$/, '');
                    // If still too long, take first meaningful part
                    if (label.length > 28) {
                        label = label.slice(0, 25) + '…';
                    }
                } else {
                    label = idStr.slice(0, 25) + '…';
                }
            }
        }

        if (!label) label = 'source';

        a.textContent = label;
        // Show full title/project/id on hover
        const tooltip = s.title || s.project || s.id || '';
        if (tooltip) a.title = tooltip;
        li.appendChild(a);
        ul.appendChild(li);
    });

    const holder = document.createElement('div');
    holder.appendChild(ul);
    answerNode.parentElement.appendChild(holder);
}

function escapeHTML(s) {
    return s.replace(/[&<>"']/g, m => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
    }[m]));
}

// Configure marked once. GFM gives us tables + autolinks; breaks=true so single
// newlines within a paragraph render as <br>, matching ChatGPT-style output.
(function configureMarked() {
    if (typeof window === 'undefined' || !window.marked || window.__faMarkedConfigured) return;
    window.marked.setOptions({
        gfm: true,
        breaks: true,
    });
    window.__faMarkedConfigured = true;
})();

// Wrap [N] citations in <sup>, but skip anything inside <code>, <pre>, or <a>
// so digit-containing code/links aren't mangled.
function applyCitationSuperscripts(html) {
    if (!html) return html;
    return html.replace(
        /(<(code|pre|a)\b[^>]*>[\s\S]*?<\/\2>)|\[(\d+)\]/g,
        (match, block, _tag, num) => block || `<sup class="cite">${num}</sup>`
    );
}

const FA_PURIFY_CONFIG = {
    ADD_ATTR: ['target', 'rel'],
    // Allow KaTeX-style spans and the citation sup we emit.
    ALLOWED_TAGS: [
        'a','b','blockquote','br','code','del','div','em','h1','h2','h3','h4','h5','h6',
        'hr','i','img','li','ol','p','pre','span','strong','sub','sup','table','tbody','td',
        'tfoot','th','thead','tr','ul'
    ],
    ALLOWED_ATTR: [
        'href','title','alt','src','class','target','rel','colspan','rowspan','align','start'
    ],
};

function attachCodeBlockEnhancements(container) {
    if (!container) return;
    container.querySelectorAll('pre > code').forEach((codeEl) => {
        if (codeEl.dataset.faEnhanced === '1') return;
        codeEl.dataset.faEnhanced = '1';
        // Highlight if the language hint is present, or just auto-detect on plain blocks.
        try {
            if (window.hljs) {
                window.hljs.highlightElement(codeEl);
            }
        } catch (_) {}
        // Wrap with a copy button.
        const pre = codeEl.parentElement;
        if (!pre || pre.querySelector('.code-copy-btn')) return;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'code-copy-btn';
        btn.textContent = 'Copy';
        btn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(codeEl.textContent || '');
                btn.textContent = 'Copied';
                setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
            } catch (_) {
                btn.textContent = 'Failed';
                setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
            }
        });
        pre.appendChild(btn);
    });
}

function renderAssistantRichText(node) {
    if (!node) return;
    // Source of truth: dataset.rawText is set by the streaming code path; for backfill
    // (history rendering) fall through to the existing textContent.
    const raw = node.dataset.rawText != null ? node.dataset.rawText : (node.textContent || '');
    node.dataset.rawText = raw;

    let html;
    try {
        html = window.marked ? window.marked.parse(raw) : escapeHTML(raw);
    } catch (e) {
        console.warn('marked parse failed, falling back to plain text', e);
        html = escapeHTML(raw);
    }

    html = applyCitationSuperscripts(html);

    if (window.DOMPurify) {
        html = window.DOMPurify.sanitize(html, FA_PURIFY_CONFIG);
    }
    node.innerHTML = html;
    attachCodeBlockEnhancements(node);
}

// Throttled re-render during streaming. We accumulate raw markdown into
// dataset.rawText and only repaint at most once per ~80ms.
function scheduleAssistantRichRender(node) {
    if (!node) return;
    if (node.__faRenderScheduled) return;
    node.__faRenderScheduled = true;
    setTimeout(() => {
        node.__faRenderScheduled = false;
        try {
            renderAssistantRichText(node);
        } catch (e) {
            // Streaming partial markdown can occasionally trip the parser; ignore mid-stream
            // and let the final 'done' render reconcile.
        }
    }, 80);
}

// -----------------------------
// SSE over fetch + ReadableStream
// -----------------------------
// EventSource doesn't allow custom headers, which forced us to put the JWT in the
// query string. fetch() + a manual SSE parser does. AbortController also gives us
// a real abort that propagates to the server (closes TCP, FastAPI sees disconnect,
// httpx stream to vLLM is cancelled), instead of EventSource.close()'s "best effort".
function createSSEConnection(url, init = {}) {
    const target = new EventTarget();
    const controller = new AbortController();
    let closed = false;

    target.close = () => {
        if (closed) return;
        closed = true;
        try { controller.abort(); } catch (_) {}
    };

    (async () => {
        let res;
        try {
            res = await fetch(url, { ...init, signal: controller.signal });
        } catch (err) {
            if (err && err.name === 'AbortError') return;
            const evt = new Event('error');
            evt.detail = err;
            target.dispatchEvent(evt);
            return;
        }
        if (!res.ok || !res.body) {
            const evt = new Event('error');
            evt.status = res.status;
            try { evt.body = await res.text(); } catch (_) {}
            target.dispatchEvent(evt);
            return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';

        try {
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });

                // Drain complete events (separator: blank line, CRLF or LF).
                let m;
                while ((m = buffer.match(/\r?\n\r?\n/))) {
                    const block = buffer.slice(0, m.index);
                    buffer = buffer.slice(m.index + m[0].length);
                    dispatchSSEBlock(block, target);
                }
            }
            buffer += decoder.decode();
            if (buffer.trim()) dispatchSSEBlock(buffer, target);
        } catch (err) {
            if (err && err.name === 'AbortError') return;
            const evt = new Event('error');
            evt.detail = err;
            target.dispatchEvent(evt);
        }
    })();

    return target;
}

function dispatchSSEBlock(block, target) {
    let event = 'message';
    const dataLines = [];
    for (const rawLine of block.split(/\r?\n/)) {
        if (!rawLine || rawLine.startsWith(':')) continue;
        const colonIdx = rawLine.indexOf(':');
        if (colonIdx < 0) continue;
        const field = rawLine.slice(0, colonIdx);
        let value = rawLine.slice(colonIdx + 1);
        if (value.startsWith(' ')) value = value.slice(1);
        if (field === 'event') event = value;
        else if (field === 'data') dataLines.push(value);
        // id/retry fields ignored — not needed for our flow.
    }
    if (dataLines.length) {
        target.dispatchEvent(new MessageEvent(event, { data: dataLines.join('\n') }));
    }
}

function getAttachedFileNames() {
    if (!uploadedFilesEl) return [];
    const names = [];
    uploadedFilesEl.querySelectorAll('.file-chip-name').forEach((el) => {
        const t = (el.textContent || '').trim();
        if (t) names.push(t);
    });
    return names;
}

function collectClientHistoryForRequest() {
    const MAX_MESSAGES = 12;
    const MAX_TOTAL_CHARS = 6000;
    if (!chat) return [];

    const rows = Array.from(chat.querySelectorAll('.msg'));
    const messages = [];

    for (const row of rows) {
        const role = row.classList.contains('you')
            ? 'user'
            : row.classList.contains('assistant')
                ? 'assistant'
                : null;
        if (!role) continue;

        const bubble = row.querySelector('.bubble');
        const content = (bubble?.textContent || '').trim();
        if (!content) continue;

        messages.push({ role, content });
    }

    const recent = messages.slice(-MAX_MESSAGES);
    let totalChars = 0;
    const bounded = [];

    for (let i = recent.length - 1; i >= 0; i -= 1) {
        const msg = recent[i];
        if (totalChars + msg.content.length > MAX_TOTAL_CHARS) break;
        bounded.push(msg);
        totalChars += msg.content.length;
    }

    return bounded.reverse();
}

function startStream(q, opts = {}) {
    const { skipUserEcho = false, replaceHistory = false } = opts;

    if (es) {
        es.close();
        es = null;
    }
    cancelled = false;
    pendingSources = [];
    statusNode = null;
    serverTimingMs = null;
    pendingGroundingMode = '';
    clientStartTs = Date.now();

    updateComposerPrimaryButton(true);
    chat.setAttribute('aria-busy', 'true');

    const sendDocIds = [...activeDocIds];
    const attachedNames = getAttachedFileNames();
    lastSentAttachments = attachedNames.map((name, idx) => ({
        doc_id: sendDocIds[idx] || null,
        filename: name,
    }));
    const userDisplay = attachedNames.length
        ? `${q}\n\nAttached PDF${attachedNames.length > 1 ? 's' : ''}: ${attachedNames.join(', ')}`
        : q;
    if (!skipUserEcho) {
        addMessage('you', userDisplay);
    }
    answerNode = addMessage('assistant', '');

    if (window.onAssistantStreamStart) {
        window.onAssistantStreamStart();
    }

    const chatBackendBase = window.CHAT_BACKEND_URL || '';

    const body = {
        q,
        page: 1,
        max_tokens: -1,
        model: selModel.value,
    };
    if (sendDocIds.length) body.doc_ids = sendDocIds;
    if (isShortAffirmation(q) && lastAssistantFollowupQuestion) {
        body.followup_hint = lastAssistantFollowupQuestion;
    }
    const clientHistory = collectClientHistoryForRequest();
    if (clientHistory.length) body.client_history = clientHistory;
    if (replaceHistory) body.replace_history = true;
    if (pausePersonalizationActive()) body.pause_personalization = true;

    // Attachments are now bound to this sent user turn; clear pending list in composer.
    activeDocIds = [];
    clearUploadedFileChips();

    const streamUrl = currentSessionUuid
        ? `${chatBackendBase}/chatbot/api/chats/${encodeURIComponent(currentSessionUuid)}/message`
        : `${chatBackendBase}/chatbot/api/chats/message`;

    debugLog('SSE POST', streamUrl, 'auth=', !!authToken);
    es = createSSEConnection(streamUrl, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...(authToken ? { 'Authorization': 'Bearer ' + authToken } : {}),
            'Accept': 'text/event-stream',
        },
        body: JSON.stringify(body),
        cache: 'no-store',
    });

    es.addEventListener('status', (e) => {
        try {
            const obj = JSON.parse(e.data);
            const isThinking = (obj.stage === 'LLM');
            setStatus(`${obj.stage}: ${obj.message}`, isThinking);
        } catch {
            setStatus(e.data, false);
        }
    });

    es.addEventListener('token', (e) => {
        const prev = answerNode.dataset.rawText || '';
        answerNode.dataset.rawText = prev + e.data;
        scheduleAssistantRichRender(answerNode);
        scrollToBottom();
        if (window.onAssistantToken) {
            window.onAssistantToken(e.data);
        }
    });

    es.addEventListener('sources', (e) => {
        try {
            const parsed = JSON.parse(e.data);
            // null or empty array means explicitly clear sources
            pendingSources = parsed || [];
            if (DEBUG && pendingSources.length) {
                console.log('Received sources:', JSON.stringify(pendingSources, null, 2));
            }
        } catch {
            pendingSources = [];
        }
    });

    es.addEventListener('grounding', (e) => {
        try {
            const parsed = JSON.parse(e.data);
            pendingGroundingMode = (parsed && parsed.mode) ? String(parsed.mode) : '';
        } catch {
            pendingGroundingMode = '';
        }
    });

    es.addEventListener('timing', (e) => {
        try {
            const t = JSON.parse(e.data);
            serverTimingMs = (t && typeof t.total_ms === 'number') ? t.total_ms : null;
        } catch {
            serverTimingMs = null;
        }
    });

    es.addEventListener('stats', () => { /* noop */ });

    es.addEventListener('done', () => {
        setStatus(cancelled ? 'Stopped.' : 'Completed.');

        renderAssistantRichText(answerNode);

        const ms = (serverTimingMs != null ? serverTimingMs : (Date.now() - clientStartTs));
        attachAssistantFooter(answerNode, ms, cancelled ? 'Stopped.' : 'Completed.');
        renderGroundingBadge(answerNode, pendingGroundingMode);
        attachResponseActions(answerNode);
        renderSourcesInline(answerNode, pendingSources);

        const assistantText = answerNode ? (answerNode.dataset.rawText || answerNode.textContent || '') : '';
        const followup = extractLastQuestion(assistantText);
        if (followup) lastAssistantFollowupQuestion = followup;
        // Fire-and-forget log to backend
        logChatTurnToBackend(lastUserQuestion, assistantText, ms);

        if (window.onAssistantStreamEnd) {
            window.onAssistantStreamEnd();
        }
        cleanup();
    });

    es.addEventListener('app_error', (e) => {
        let msg = 'Request failed.';
        try {
            const obj = JSON.parse(e.data || '{}');
            if (obj && obj.message) msg = obj.message;
        } catch {}
        setStatus(msg);
        if (window.onAssistantStreamEnd) {
            window.onAssistantStreamEnd();
        }
        cleanup();
    });

    es.addEventListener('error', () => {
        setStatus('Connection interrupted. Please retry.');
        if (window.onAssistantStreamEnd) {
            window.onAssistantStreamEnd();
        }
        cleanup();
    });
}

function cleanup() {
    if (es) {
        es.close();
        es = null;
    }
    updateComposerPrimaryButton(false);
    chat.setAttribute('aria-busy', 'false');
    if (statusNode) statusNode.classList.remove('blink');
    if (window.speechSynthesis) {
        window.speechSynthesis.cancel();
    }
    scrollToBottom();
}

async function uploadPdfFile(file) {
    if (!file || !authToken) return;

    if (uploadPdfBtn) {
        uploadPdfBtn.disabled = true;
        uploadPdfBtn.classList.add('is-loading');
        uploadPdfBtn.setAttribute('aria-busy', 'true');
        uploadPdfBtn.title = 'Uploading PDF...';
    }

    const tempId = `tmp-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    const pendingChip = renderUploadedFileChip(tempId, file.name || 'document.pdf', 'Uploading...');

    const form = new FormData();
    form.append('file', file);

    try {
        const res = await fetch(PDF_UPLOAD_URL, {
            method: 'POST',
            headers: {
                'Authorization': 'Bearer ' + authToken,
            },
            body: form,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.status !== 'success') {
            console.warn('PDF upload failed', res.status, data);
            if (pendingChip) {
                const st = pendingChip.querySelector('.file-chip-status');
                if (st) st.textContent = 'Failed';
            }
            return;
        }

        if (data.doc_id && !activeDocIds.includes(data.doc_id)) {
            activeDocIds.push(data.doc_id);
        }

        const filename = data.filename || file.name || 'document.pdf';
        if (pendingChip) {
            pendingChip.dataset.docId = data.doc_id || tempId;
            const nm = pendingChip.querySelector('.file-chip-name');
            const st = pendingChip.querySelector('.file-chip-status');
            if (nm) nm.textContent = filename;
            if (st) st.textContent = 'Uploaded';
        } else {
            renderUploadedFileChip(data.doc_id, filename, 'Uploaded');
        }
    } catch (err) {
        console.error('Error uploading PDF', err);
        if (pendingChip) {
            const st = pendingChip.querySelector('.file-chip-status');
            if (st) st.textContent = 'Failed';
        }
    } finally {
        if (uploadPdfBtn) {
            uploadPdfBtn.disabled = false;
            uploadPdfBtn.classList.remove('is-loading');
            uploadPdfBtn.removeAttribute('aria-busy');
            uploadPdfBtn.title = 'Upload PDF';
        }
    }
}

async function deleteUploadedPdf(docId) {
    if (!docId || !authToken) return;
    try {
        const res = await fetch(`/files/pdf/${docId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': 'Bearer ' + authToken,
            },
        });
        if (!res.ok) {
            console.warn('Failed to delete uploaded PDF', res.status);
        }
    } catch (err) {
        console.error('Error deleting uploaded PDF', err);
    }
}

function renderUploadedFileChip(docId, filename, statusText) {
    if (!uploadedFilesEl || !docId) return;
    const existing = uploadedFilesEl.querySelector(`[data-doc-id="${docId}"]`);
    if (existing) return existing;

    uploadedFilesEl.classList.remove('hidden');

    const chip = document.createElement('div');
    chip.className = 'file-chip';
    chip.dataset.docId = docId;

    const name = document.createElement('span');
    name.className = 'file-chip-name';
    name.textContent = filename || 'document.pdf';

    const st = document.createElement('span');
    st.className = 'file-chip-status';
    st.textContent = statusText || 'Uploaded';

    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'file-chip-remove';
    rm.title = 'Remove file';
    rm.textContent = '×';
    rm.addEventListener('click', async () => {
        activeDocIds = activeDocIds.filter(x => x !== docId);
        chip.remove();
        if (!uploadedFilesEl.children.length) uploadedFilesEl.classList.add('hidden');
        await deleteUploadedPdf(docId);
    });

    chip.appendChild(name);
    chip.appendChild(st);
    chip.appendChild(rm);
    uploadedFilesEl.appendChild(chip);
    return chip;
}

if (uploadPdfBtn && pdfFileInput) {
    uploadPdfBtn.addEventListener('click', () => {
        pdfFileInput.click();
    });
    pdfFileInput.addEventListener('change', async () => {
        const f = (pdfFileInput.files && pdfFileInput.files[0]) || null;
        if (f) await uploadPdfFile(f);
        pdfFileInput.value = '';
    });
}

// Submit handler: ensure session exists, then stream
if (form) {
    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        if (es) {
            cancelled = true;
            cleanup();
            return;
        }
        const q = input.value.trim();
        if (!q) return;
        if (q.length > MAX_QUESTION_CHARS) {
            window.alert(`Question is too long. Maximum ${MAX_QUESTION_CHARS} characters.`);
            return;
        }

        lastUserQuestion = q;

        // If user reopened an old empty untitled session, start a titled one instead.
        await maybeReseedUntitledEmptySession(q);

        // Make sure there is a ChatSession row for this conversation
        await ensureSessionExists(q);

        startStream(q);
        input.value = '';
        input.style.height = 'auto';
        input.focus();
    });
}

// Convenience: Enter sends, Shift+Enter adds newline
if (input && form) {
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            form.requestSubmit();
        }
    });
}

updateComposerPrimaryButton(false);

// Initial load of sessions when the page opens
(async function initChatPage() {
    await loadSessions().catch(console.error);
    const chatId = getChatIdFromUrl();
    if (chatId) {
        await openSession(chatId).catch(console.error);
    }
})();

// -----------------------------
// Pause personalization (memory off for next message)
// -----------------------------
const PAUSE_MEMORY_LS_KEY = 'fa_pause_personalization';

function pausePersonalizationActive() {
    return localStorage.getItem(PAUSE_MEMORY_LS_KEY) === '1';
}

function setPausePersonalization(value) {
    if (value) {
        localStorage.setItem(PAUSE_MEMORY_LS_KEY, '1');
    } else {
        localStorage.removeItem(PAUSE_MEMORY_LS_KEY);
    }
    syncPauseMemoryButton();
}

function syncPauseMemoryButton() {
    const btn = document.getElementById('pause-memory-btn');
    if (!btn) return;
    const active = pausePersonalizationActive();
    btn.classList.toggle('is-active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    btn.title = active
        ? 'Memory is paused for the next message. Click to resume.'
        : 'Pause memory for the next message';
}

(function wirePauseMemoryButton() {
    const btn = document.getElementById('pause-memory-btn');
    if (!btn) return;
    btn.addEventListener('click', () => setPausePersonalization(!pausePersonalizationActive()));
    syncPauseMemoryButton();
})();

// -----------------------------
// Memory management modal
// -----------------------------
const MEMORY_BASE = '/chatbot/api/users/me/memory';

async function fetchMemoryNotes() {
    if (!authToken) return [];
    try {
        const res = await fetch(`${MEMORY_BASE}?limit=50`, {
            method: 'GET',
            headers: backendHeaders(),
        });
        if (!res.ok) return [];
        const data = await res.json().catch(() => ({}));
        return Array.isArray(data?.results) ? data.results : [];
    } catch (e) {
        console.warn('Failed to load memory notes', e);
        return [];
    }
}

async function deleteMemoryNote(noteId) {
    try {
        const res = await fetch(`${MEMORY_BASE}/${encodeURIComponent(noteId)}`, {
            method: 'DELETE',
            headers: backendHeaders(),
        });
        return res.ok;
    } catch (e) {
        console.warn('Failed to delete memory note', e);
        return false;
    }
}

function renderMemoryList(notes) {
    const body = document.getElementById('memory-modal-body');
    if (!body) return;
    if (!notes.length) {
        body.innerHTML = `<div class="memory-empty">Nothing remembered yet. Ask substantive questions and the assistant will start building a memory.</div>`;
        return;
    }
    body.innerHTML = '';
    const list = document.createElement('ul');
    list.className = 'memory-list';
    notes.forEach((note) => {
        const li = document.createElement('li');
        li.className = 'memory-item';
        li.dataset.noteId = String(note.id);

        const text = document.createElement('div');
        text.className = 'memory-text';
        text.textContent = note.note_text || '';

        const meta = document.createElement('div');
        meta.className = 'memory-meta';
        const conf = typeof note.confidence === 'number'
            ? `${Math.round(note.confidence * 100)}% confidence`
            : '';
        const lang = note.source_language && note.source_language !== 'en'
            ? ` · ${String(note.source_language).toUpperCase()}`
            : '';
        meta.textContent = `${conf}${lang}`.trim();

        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'memory-delete-btn';
        del.title = 'Forget this';
        del.setAttribute('aria-label', 'Forget this');
        del.textContent = 'Forget';
        del.addEventListener('click', async () => {
            del.disabled = true;
            const ok = await deleteMemoryNote(note.id);
            if (ok) {
                li.remove();
                if (!document.querySelector('.memory-list .memory-item')) {
                    renderMemoryList([]);
                }
            } else {
                del.disabled = false;
                del.textContent = 'Failed';
                setTimeout(() => { del.textContent = 'Forget'; }, 1500);
            }
        });

        li.appendChild(text);
        li.appendChild(meta);
        li.appendChild(del);
        list.appendChild(li);
    });
    body.appendChild(list);
}

async function openMemoryModal() {
    const modal = document.getElementById('memory-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    modal.setAttribute('aria-hidden', 'false');
    const body = document.getElementById('memory-modal-body');
    if (body) body.innerHTML = `<div class="memory-empty">Loading…</div>`;
    const notes = await fetchMemoryNotes();
    renderMemoryList(notes);
}

function closeMemoryModal() {
    const modal = document.getElementById('memory-modal');
    if (!modal) return;
    modal.classList.add('hidden');
    modal.setAttribute('aria-hidden', 'true');
}

(function wireMemoryModal() {
    const openBtn = document.getElementById('memory-btn');
    if (openBtn) {
        openBtn.addEventListener('click', openMemoryModal);
    }
    document.querySelectorAll('[data-close-memory]').forEach((el) => {
        el.addEventListener('click', closeMemoryModal);
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeMemoryModal();
    });
})();

window.addEventListener('popstate', async () => {
    const chatId = getChatIdFromUrl();
    if (chatId) {
        await openSession(chatId).catch(console.error);
        return;
    }

    currentSessionUuid = null;
    currentSessionTitle = '';
    currentSessionMessageCount = 0;
    activeDocIds = [];
    clearUploadedFileChips();
    if (chat) chat.innerHTML = '';
});
