// static/js/voice.js
// Voice Manager: Handles Speech-to-Text (STT) and Text-to-Speech (TTS)
// Supports all EU languages with automatic language detection

(function() {
    'use strict';
    const DEBUG = false;

    function debugLog(...args) {
        if (DEBUG) console.log(...args);
    }

    function getVoiceSvgIcon(name) {
        const icons = {
            speaker: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 9v6h4l5 4V5L9 9z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M18 9a4 4 0 0 1 0 6M20.5 6.5a7.5 7.5 0 0 1 0 11" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
            mic: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3a3 3 0 0 1 3 3v6a3 3 0 1 1-6 0V6a3 3 0 0 1 3-3Z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M19 11a7 7 0 0 1-14 0M12 18v3M8 21h8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
            pause: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14M16 5v14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
            play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
            stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>',
        };
        return icons[name] || '';
    }

    // =========================
    // EU Language Support
    // =========================
    const EU_LANGUAGES = {
        'bg': { name: 'Bulgarian', voiceLang: 'bg-BG' },
        'hr': { name: 'Croatian', voiceLang: 'hr-HR' },
        'cs': { name: 'Czech', voiceLang: 'cs-CZ' },
        'da': { name: 'Danish', voiceLang: 'da-DK' },
        'nl': { name: 'Dutch', voiceLang: 'nl-NL' },
        'en': { name: 'English', voiceLang: 'en-GB' },
        'et': { name: 'Estonian', voiceLang: 'et-EE' },
        'fi': { name: 'Finnish', voiceLang: 'fi-FI' },
        'fr': { name: 'French', voiceLang: 'fr-FR' },
        'de': { name: 'German', voiceLang: 'de-DE' },
        'el': { name: 'Greek', voiceLang: 'el-GR' },
        'hu': { name: 'Hungarian', voiceLang: 'hu-HU' },
        'ga': { name: 'Irish', voiceLang: 'ga-IE' },
        'it': { name: 'Italian', voiceLang: 'it-IT' },
        'lv': { name: 'Latvian', voiceLang: 'lv-LV' },
        'lt': { name: 'Lithuanian', voiceLang: 'lt-LT' },
        'mt': { name: 'Maltese', voiceLang: 'mt-MT' },
        'pl': { name: 'Polish', voiceLang: 'pl-PL' },
        'pt': { name: 'Portuguese', voiceLang: 'pt-PT' },
        'ro': { name: 'Romanian', voiceLang: 'ro-RO' },
        'sk': { name: 'Slovak', voiceLang: 'sk-SK' },
        'sl': { name: 'Slovenian', voiceLang: 'sl-SI' },
        'es': { name: 'Spanish', voiceLang: 'es-ES' },
        'sv': { name: 'Swedish', voiceLang: 'sv-SE' }
    };

    // =========================
    // TTS Manager
    // =========================
    const TTSManager = {
        synthesis: window.speechSynthesis,
        currentUtterance: null,
        isPlaying: false,
        isPaused: false,
        textQueue: [],
        currentText: '',
        currentPosition: 0,
        controlContainer: null,
        voices: [],        // Available voices
        currentVoice: null,

        // Initialize TTS controls
        init() {
            if (!this.synthesis) {
                console.warn('TTS not supported in this browser');
                return;
            }
            
            // Load voices (may take time in some browsers)
            this.loadVoices();
            if (this.synthesis.onvoiceschanged !== undefined) {
                this.synthesis.onvoiceschanged = () => this.loadVoices();
            }
            
            this.createControls();
        },

        // Load available voices
        loadVoices() {
            this.voices = this.synthesis.getVoices() || [];
            debugLog(`Loaded ${this.voices.length} TTS voices`);
            
            // Log available languages
            const uniqueLangs = [...new Set(this.voices.map(v => v.lang.split('-')[0]))];
            debugLog('Available voice languages:', uniqueLangs.join(', '));
        },

        // Create floating TTS controls
        createControls() {
            const container = document.createElement('div');
            container.id = 'tts-controls';
            container.className = 'tts-controls hidden';
            container.innerHTML = `
                <div class="tts-control-bar">
                    <span class="tts-status">🔊 Playing</span>
                    <div class="tts-buttons">
                        <button id="tts-pause" class="tts-btn" type="button" title="Pause" aria-label="Pause">
                            ${getVoiceSvgIcon('pause')}
                        </button>
                        <button id="tts-stop" class="tts-btn" type="button" title="Stop" aria-label="Stop">
                            ${getVoiceSvgIcon('stop')}
                        </button>
                    </div>
                </div>
            `;
            document.body.appendChild(container);
            this.controlContainer = container;

            // Event listeners
            container.querySelector('#tts-pause').addEventListener('click', () => this.togglePause());
            container.querySelector('#tts-stop').addEventListener('click', () => this.stop());
        },

        updatePlaybackButtons() {
            const pauseBtn = this.controlContainer?.querySelector('#tts-pause');
            if (!pauseBtn) return;
            if (this.isPaused) {
                pauseBtn.innerHTML = getVoiceSvgIcon('play');
                pauseBtn.title = 'Resume';
                pauseBtn.setAttribute('aria-label', 'Resume');
            } else {
                pauseBtn.innerHTML = getVoiceSvgIcon('pause');
                pauseBtn.title = 'Pause';
                pauseBtn.setAttribute('aria-label', 'Pause');
            }
        },

        // Show/hide controls
        showControls() {
            if (this.controlContainer) {
                this.controlContainer.classList.remove('hidden');
            }
        },

        hideControls() {
            if (this.controlContainer) {
                this.controlContainer.classList.add('hidden');
            }
        },

        updateStatus(text) {
            const status = this.controlContainer?.querySelector('.tts-status');
            if (status) status.textContent = `🔊 ${text}`;
        },

        // Detect language from text (simple heuristic)
        detectLanguage(text) {
            // Try to detect based on common words and characters
            const langPatterns = {
                'de': /\b(der|die|das|ein|eine|und|ist|sind|von|für|mit|auf)\b/i,
                'fr': /\b(le|la|les|un|une|et|est|sont|pour|avec|sur|dans)\b/i,
                'es': /\b(el|la|los|las|un|una|y|es|son|por|con|en)\b/i,
                'it': /\b(il|la|i|le|un|una|e|è|sono|per|con|su)\b/i,
                'nl': /\b(de|het|een|en|is|zijn|van|voor|met|op)\b/i,
                'pl': /\b(ogółem|wykorzystanie|przestrzeń|dostęp|jakość)\b/i,
                'ro': /\b(sunt|pentru|cu|din|mai|poate|fost|sau)\b/i,
                'el': /[α-ωΑ-Ω]/,
                'bg': /[а-ъьюяА-ЪЬЮЯ]/,
                'sv': /\b(och|för|med|på|att|den|det|som|av|från)\b/i,
                'da': /\b(og|for|med|på|at|den|det|som|af|fra)\b/i,
                'fi': /\b(ja|on|varten|kanssa|että|se|ovat|kuin)\b/i,
                'et': /\b(ja|on|jaoks|koos|et|see|on|nagu)\b/i,
                'hu': /\b(és|a|az|hogy|van|egy|meg|ez)\b/i,
                'cs': /\b(a|je|pro|s|na|že|to|se|v)\b/i,
                'sk': /\b(a|je|pre|s|na|že|to|sa|v)\b/i,
                'sl': /\b(in|je|za|s|na|da|to|se|v)\b/i,
                'hr': /\b(i|je|za|s|na|da|to|se|u)\b/i,
                'pt': /\b(o|a|os|as|um|uma|e|é|são|por|com|em)\b/i,
                'lt': /\b(ir|yra|už|su|kad|tai|si|į)\b/i,
                'lv': /\b(un|ir|par|ar|ka|tas|se|uz)\b/i,
                'mt': /\b(u|huwa|għal|ma'|li|dan|hu|f')/i,
                'ga': /\b(agus|tá|do|le|go|an|sé|i)\b/i,
            };

            for (const [lang, pattern] of Object.entries(langPatterns)) {
                if (pattern.test(text)) {
                    return EU_LANGUAGES[lang] ? EU_LANGUAGES[lang].voiceLang : 'en-GB';
                }
            }

            // Default to English if no pattern matches
            return 'en-GB';
        },

        // Get the best voice for a language
        getBestVoice(langCode) {
            if (!this.voices.length) {
                this.loadVoices();
            }
            
            const langPrefix = langCode.toLowerCase().split('-')[0];
            
            // Priority order for voice selection:
            // 1. Exact language match (e.g., en-GB)
            // 2. Prefix match with native/standard voice
            // 3. Any voice with matching prefix
            
            let voice = this.voices.find(v => v.lang.toLowerCase() === langCode.toLowerCase());
            if (voice) return voice;
            
            // Prefer native voices (Google, Apple, Microsoft)
            const nativeVoice = this.voices.find(v => {
                const vLang = v.lang.toLowerCase();
                const isMatch = vLang.startsWith(langPrefix);
                const isNative = /Google|Apple|Microsoft/.test(v.name);
                return isMatch && isNative;
            });
            if (nativeVoice) return nativeVoice;
            
            // Any voice with matching prefix
            voice = this.voices.find(v => v.lang.toLowerCase().startsWith(langPrefix));
            if (voice) return voice;
            
            // Fallback to default
            return this.voices[0];
        },

        // Speak text with controls
        speak(text) {
            if (!this.synthesis) {
                console.error('TTS not supported');
                return;
            }

            // Cancel any ongoing speech
            this.stop();

            // Ensure voices are loaded (Chrome loads them asynchronously)
            if (!this.voices || this.voices.length === 0) {
                this.loadVoices();
            }

            this.currentText = text;
            this.currentPosition = 0;
            this.isPlaying = true;
            this.isPaused = false;

            const utterance = new SpeechSynthesisUtterance(text);
            
            // Determine language automatically from the text
            const targetLang = this.detectLanguage(text);
            debugLog('TTS target language:', targetLang);
            
            // Get the best voice for this language
            const voice = this.getBestVoice(targetLang);
            
            if (voice) {
                utterance.voice = voice;
                debugLog(`TTS using voice: ${voice.name} (${voice.lang})`);
            } else {
                console.warn('No voice found for language:', targetLang, '- using default');
            }
            
            utterance.lang = targetLang;
            utterance.rate = 1.0;
            utterance.pitch = 1.0;
            
            // Event handlers
            utterance.onstart = () => {
                this.showControls();
                this.updateStatus('Playing');
                this.updatePlaybackButtons();
            };

            utterance.onpause = () => {
                this.isPaused = true;
                this.updateStatus('Paused');
                this.updatePlaybackButtons();
            };

            utterance.onresume = () => {
                this.isPaused = false;
                this.updateStatus('Playing');
                this.updatePlaybackButtons();
            };

            utterance.onend = () => {
                this.isPlaying = false;
                this.isPaused = false;
                this.updatePlaybackButtons();
                this.hideControls();
            };

            utterance.onerror = (e) => {
                console.error('TTS Error:', e);
                this.isPlaying = false;
                this.isPaused = false;
                this.updatePlaybackButtons();
                this.hideControls();
            };

            this.currentUtterance = utterance;
            this.synthesis.speak(utterance);
        },

        // Toggle pause/resume
        togglePause() {
            if (!this.synthesis) return;

            if (this.isPaused) {
                this.synthesis.resume();
            } else {
                this.synthesis.pause();
            }
        },

        // Stop speaking
        stop() {
            if (!this.synthesis) return;
            this.synthesis.cancel();
            this.isPlaying = false;
            this.isPaused = false;
            this.updatePlaybackButtons();
            this.hideControls();
        }
    };

    // =========================
    // STT Manager (Speech-to-Text)
    // =========================
    const STTManager = {
        recognition: null,
        isListening: false,
        currentLang: 'en-GB',
        inputField: null,
        micButton: null,
        listeningIndicator: null,

        // Check if STT is supported
        isSupported() {
            return 'webkitSpeechRecognition' in window || 'SpeechRecognition' in window;
        },

        // Initialize STT
        init(inputField) {
            if (!this.isSupported()) {
                console.warn('STT not supported in this browser');
                return false;
            }

            this.inputField = inputField;
            this.currentLang = this.detectPreferredLanguage();
            this.createMicButton();
            this.setupRecognition();
            return true;
        },

        detectPreferredLanguage() {
            const browserLang = String(navigator.language || navigator.userLanguage || 'en-GB').trim();
            if (!browserLang) return 'en-GB';

            const normalized = browserLang.replace('_', '-');
            const exact = Object.values(EU_LANGUAGES).find((lang) => lang.voiceLang.toLowerCase() === normalized.toLowerCase());
            if (exact) return exact.voiceLang;

            const prefix = normalized.toLowerCase().split('-')[0];
            if (EU_LANGUAGES[prefix]) {
                return EU_LANGUAGES[prefix].voiceLang;
            }

            return 'en-GB';
        },

        // Create microphone button
        createMicButton() {
            const btn = document.createElement('button');
            btn.id = 'stt-mic-btn';
            btn.type = 'button';
            btn.className = 'stt-mic-btn';
            btn.innerHTML = getVoiceSvgIcon('mic');
            btn.title = 'Click to speak (hold for continuous)';
            btn.setAttribute('aria-label', 'Voice input');

            // Insert after input field
            if (this.inputField && this.inputField.parentNode) {
                this.inputField.parentNode.insertBefore(btn, this.inputField.nextSibling);
                this.micButton = btn;

                const indicator = document.createElement('div');
                indicator.className = 'stt-listening-indicator hidden';
                indicator.setAttribute('aria-hidden', 'true');
                indicator.innerHTML = `
                    <span class="stt-wave-bar"></span>
                    <span class="stt-wave-bar"></span>
                    <span class="stt-wave-bar"></span>
                    <span class="stt-wave-label">Listening</span>
                `;
                btn.insertAdjacentElement('afterend', indicator);
                this.listeningIndicator = indicator;

                // Event listeners
                btn.addEventListener('click', () => this.toggleListening());
                
                // Touch support for mobile
                btn.addEventListener('touchstart', (e) => {
                    e.preventDefault();
                    this.startListening();
                });
                btn.addEventListener('touchend', (e) => {
                    e.preventDefault();
                    this.stopListening();
                });
            }
        },

        // Setup recognition
        setupRecognition() {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            this.recognition = new SpeechRecognition();
            this.recognition.continuous = true;
            this.recognition.interimResults = true;
            this.recognition.maxAlternatives = 1;

            this.recognition.onstart = () => {
                this.isListening = true;
                this.updateMicButton();
            };

            this.recognition.onend = () => {
                this.isListening = false;
                this.updateMicButton();
            };

            this.recognition.onresult = (event) => {
                let finalTranscript = '';
                let interimTranscript = '';

                for (let i = event.resultIndex; i < event.results.length; i++) {
                    const transcript = event.results[i][0].transcript;
                    if (event.results[i].isFinal) {
                        finalTranscript += transcript;
                    } else {
                        interimTranscript += transcript;
                    }
                }

                // Update input field
                if (this.inputField) {
                    if (finalTranscript) {
                        this.inputField.value += (this.inputField.value ? ' ' : '') + finalTranscript;
                        this.inputField.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                    // Show interim results with styling
                    if (interimTranscript) {
                        this.inputField.placeholder = `Listening: ${interimTranscript}`;
                    }
                }
            };

            this.recognition.onerror = (event) => {
                console.error('STT Error:', event.error);
                if (event.error === 'not-allowed') {
                    alert('Please allow microphone access to use voice input.');
                }
                this.isListening = false;
                this.updateMicButton();
            };
        },

        // Update mic button appearance
        updateMicButton() {
            if (!this.micButton) return;
            
            if (this.isListening) {
                this.micButton.classList.add('listening');
                this.micButton.title = 'Click to stop listening';
                if (this.listeningIndicator) {
                    this.listeningIndicator.classList.remove('hidden');
                }
            } else {
                this.micButton.classList.remove('listening');
                this.micButton.title = 'Click to speak';
                if (this.listeningIndicator) {
                    this.listeningIndicator.classList.add('hidden');
                }
                if (this.inputField) {
                    this.inputField.placeholder = 'Ask a question…';
                }
            }
        },

        // Set language for recognition
        setLanguage(langCode) {
            const lang = EU_LANGUAGES[langCode];
            if (lang) {
                this.currentLang = lang.voiceLang;
                if (this.recognition) {
                    this.recognition.lang = this.currentLang;
                }
            }
        },

        // Start listening
        startListening() {
            if (!this.recognition) return;
            
            try {
                this.recognition.lang = this.currentLang;
                this.recognition.start();
            } catch (e) {
                console.error('Failed to start recognition:', e);
            }
        },

        // Stop listening
        stopListening() {
            if (!this.recognition) return;
            
            try {
                this.recognition.stop();
            } catch (e) {
                console.error('Failed to stop recognition:', e);
            }
        },

        // Toggle listening state
        toggleListening() {
            if (this.isListening) {
                this.stopListening();
            } else {
                this.startListening();
            }
        }
    };

    // =========================
    // Integration with chat.js
    // =========================

    // Override the attachSpeakButton function in chat.js
    window.attachSpeakButton = function(bubble) {
        if (!bubble || !window.speechSynthesis) return;

        const container = bubble.parentElement;
        if (!container) return;

        // Remove existing button
        const existing = container.querySelector('.speak-btn');
        if (existing) existing.remove();

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'speak-btn';
        btn.innerHTML = getVoiceSvgIcon('speaker');
        btn.title = 'Listen to this answer';

        btn.addEventListener('click', () => {
            const text = bubble.textContent || '';
            if (!text.trim()) return;

            // Use the enhanced TTS manager
            TTSManager.speak(text);
        });

        if (container.querySelector('.status')) {
            container.querySelector('.status').appendChild(btn);
        } else {
            container.appendChild(btn);
        }
    };

    // Override the TTS callbacks
    window.onAssistantStreamStart = function() {
        TTSManager.stop();
    };

    window.onAssistantStreamEnd = function() {
        // Don't auto-stop, let user control playback
    };

    // =========================
    // Auto-initialize
    // =========================
    document.addEventListener('DOMContentLoaded', () => {
        // Initialize TTS
        TTSManager.init();

        // Initialize STT when input field is available
        const inputField = document.getElementById('question');
        if (inputField) {
            STTManager.init(inputField);
        }
    });

    // Expose TTSManager globally for attachSpeakButton
    window.TTSManager = TTSManager;
    window.STTManager = STTManager;
    window.VoiceManager = { TTS: TTSManager, STT: STTManager, EU_LANGUAGES };
})();
