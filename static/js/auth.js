// static/js/auth.js
//
// Login-page only:
// - Handles login form submission
// - Stores access/refresh tokens + user info in localStorage
// - Redirects to /chat once logged in
// - If already logged in, skips login and goes straight to /chat

// Simple storage keys (must match chat.js)
const LS_ACCESS     = 'fa_access_token';
const LS_REFRESH    = 'fa_refresh_token';
const LS_USER_UUID  = 'fa_user_uuid';
const LS_USER_EMAIL = 'fa_user_email';
const LS_EMAIL_LEGACY = 'fa_email'; // used by chat.js for header display

// DOM elements used on the login page
const loginForm    = document.getElementById('login-form');
const loginStatus  = document.getElementById('login-status');
const authInfo     = document.getElementById('auth-info');      // optional
const authUserEmail = document.getElementById('auth-user-email'); // optional
const logoutBtn    = document.getElementById('logout-btn');     // optional
const appLayout    = document.getElementById('app-layout');     // optional (usually only on /chat)

// --- Helpers ---------------------------------------------------------

function restoreAuth() {
    const existingAccess = localStorage.getItem(LS_ACCESS);
    const existingUuid   = localStorage.getItem(LS_USER_UUID);

    // If we already have a token + uuid, consider the user logged in
    // and send them straight to the chat page.
    if (existingAccess && existingUuid) {
        window.location.href = "/chat";
        return;
    }

    // Not logged in: ensure login form is visible
    if (appLayout) appLayout.classList.add('hidden');
    if (loginForm) loginForm.classList.remove('hidden');
    if (authInfo) authInfo.classList.add('hidden');
}

// --- Login -----------------------------------------------------------

if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const email = document.getElementById('login-email').value.trim();
        const password = document.getElementById('login-password').value;

        if (!email || !password) {
            if (loginStatus) loginStatus.textContent = 'Email and password are required';
            return;
        }

        if (loginStatus) loginStatus.textContent = 'Logging in…';

        try {
            // FastAPI login endpoint; this calls through to Django
            const res = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password }),
            });

            const data = await res.json().catch(() => ({}));

            if (!res.ok || data.status !== 'success') {
                if (loginStatus) {
                    loginStatus.textContent = data.message || 'Login failed';
                }
                return;
            }

            // Expecting access_token, refresh_token, uuid in response
            const accessToken  = data.access_token;
            const refreshToken = data.refresh_token;
            const userUuid     = data.uuid;

            // Persist tokens + email so chat.js can pick them up
            localStorage.setItem(LS_ACCESS, accessToken || '');
            localStorage.setItem(LS_REFRESH, refreshToken || '');
            localStorage.setItem(LS_USER_UUID, userUuid || '');
            localStorage.setItem(LS_USER_EMAIL, email);
            // Legacy key used by chat.js for greeting
            localStorage.setItem(LS_EMAIL_LEGACY, email);

            if (loginStatus) loginStatus.textContent = 'Logged in';

            // If you show "logged-in" header on this page, update it
            if (authInfo) authInfo.classList.remove('hidden');
            if (authUserEmail) authUserEmail.textContent = email;

            // Main behaviour: go to chat UI, where chat.js will:
            // - read fa_access_token / fa_refresh_token
            // - load sessions
            window.location.href = "/chat";
        } catch (err) {
            console.error(err);
            if (loginStatus) loginStatus.textContent = 'Error during login';
        }
    });
}

// --- Logout (optional on login page) ---------------------------------

if (logoutBtn) {
    logoutBtn.addEventListener('click', () => {
        // Prefer the central forceLogout from chat.js if available
        if (typeof forceLogout === 'function') {
            forceLogout('user_clicked_logout_from_login_page');
            return;
        }

        // Fallback: minimal local clear + redirect
        localStorage.removeItem(LS_ACCESS);
        localStorage.removeItem(LS_REFRESH);
        localStorage.removeItem(LS_USER_UUID);
        localStorage.removeItem(LS_USER_EMAIL);
        localStorage.removeItem(LS_EMAIL_LEGACY);
        window.location.href = "/";
    });
}

// Run initial restore on page load
restoreAuth();
