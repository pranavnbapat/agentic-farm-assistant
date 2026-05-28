// static/js/login.js

// Storage keys (must match chat.js)
const LS_TOKEN       = 'fa_access_token';
const LS_REFRESH     = 'fa_refresh_token';
const LS_USER_UUID   = 'fa_user_uuid';
const LS_USER_EMAIL  = 'fa_user_email';
// Legacy key that chat.js still reads as LS_EMAIL
const LS_EMAIL       = 'fa_email';

document.addEventListener("DOMContentLoaded", () => {
    const form     = document.getElementById("login-form");
    const statusEl = document.getElementById("login-status");

    if (!form) return;  // safety guard

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        statusEl.textContent = "Logging in…";

        const email    = document.getElementById("login-email").value.trim();
        const password = document.getElementById("login-password").value;

        try {
            const resp = await fetch("/api/login", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({ email, password }),
            });

            if (!resp.ok) {
                // Non-200 from FastAPI
                let errorMsg = "Login failed";
                try {
                    const errorData = await resp.json();
                    errorMsg = errorData.detail || errorData.message || `Login failed (${resp.status})`;
                } catch (e) {
                    // If JSON parsing fails, try to get raw text
                    const text = await resp.text().catch(() => "");
                    if (text) errorMsg = text.substring(0, 200);
                }
                console.error("Login error:", resp.status, errorMsg);
                statusEl.textContent = errorMsg;
                return;
            }

            const data = await resp.json();
            console.log("Login response:", data);

            // Django /fastapi/login returns:
            // { access_token, refresh_token, uuid, status: "success" }
            const access  = data.access_token || data.token;
            const refresh = data.refresh_token || "";
            const uuid    = data.uuid || "";
            const emailToStore = data.email || email;

            if (!access) {
                console.error("No access_token in login response", data);
                statusEl.textContent = "Login failed (no token)";
                return;
            }

            // Persist everything for chat.js
            localStorage.setItem(LS_TOKEN, access);          // fa_access_token
            localStorage.setItem(LS_REFRESH, refresh);       // fa_refresh_token
            localStorage.setItem(LS_USER_UUID, uuid);        // fa_user_uuid
            localStorage.setItem(LS_USER_EMAIL, emailToStore); // fa_user_email

            // Legacy keys used by existing chat.js header
            localStorage.setItem(LS_EMAIL, emailToStore);    // fa_email

            statusEl.textContent = "Logged in";

            // Go to chat page
            window.location.href = "/chat";
        } catch (err) {
            console.error("Login exception:", err);
            statusEl.textContent = "Network error, please try again.";
        }
    });
});
