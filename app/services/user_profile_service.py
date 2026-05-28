# app/services/user_profile_service.py
"""
User Profile & Memory Service with multi-language support.

This service builds and maintains user profiles by:
1. Detecting the language of user queries
2. Using LLM-based extraction to understand queries in any language
3. Normalizing extracted facts to English for consistent storage
4. Deduplicating and merging facts intelligently
5. Providing context for personalized responses
"""

import logging
import re
import json
import hashlib
from datetime import datetime
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

import httpx

from app.config import get_settings
from app.utils.language_utils import detect_language, get_language_name

# Try to import LLM client
try:
    from app.clients.vllm_client import generate_once
    VLLM_AVAILABLE = True
except ImportError:
    VLLM_AVAILABLE = False

S = get_settings()
logger = logging.getLogger("farm-assistant.profile")

CHAT_BACKEND_URL = (S.CHAT_BACKEND_URL or "").rstrip("/")


@dataclass
class UserProfile:
    """Structured user profile data."""
    user_uuid: str
    expertise_level: str = "beginner"
    farm_type: Optional[str] = None
    region: Optional[str] = None
    preferred_language: str = "en"
    communication_style: str = "detailed"
    crops: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    query_languages: List[str] = field(default_factory=list)
    total_queries: int = 0
    first_seen: Optional[str] = None
    last_updated: Optional[str] = None


@dataclass
class UserFact:
    """A specific fact about the user."""
    category: str
    text: str
    confidence: float = 1.0
    source_language: str = "en"
    source_query_hash: Optional[str] = None
    session_uuid: Optional[str] = None
    extracted_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "text": self.text,
            "confidence": self.confidence,
            "source_language": self.source_language,
            "source_query_hash": self.source_query_hash,
            "session_uuid": self.session_uuid,
            "extracted_at": self.extracted_at or datetime.utcnow().isoformat(),
        }


@dataclass
class ExtractedProfile:
    """Result of profile extraction from a single message."""
    expertise_level: Optional[str] = None
    farm_type: Optional[str] = None
    region: Optional[str] = None
    crops: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    facts: List[UserFact] = field(default_factory=list)
    # Free-form memory notes — stable, user-centric details worth remembering
    # across conversations that do not fit the structured fact categories.
    # These should be long-lived preferences or identity/context signals, not
    # turn summaries.
    memory_notes: List[str] = field(default_factory=list)
    communication_style: Optional[str] = None
    preferred_language: Optional[str] = None
    detected_language: str = "en"


class UserProfileService:
    """
    Service for managing user profiles with multi-language support.

    - LLM-based extraction works with any of the EU languages.
    - Language is detected per-query and tracked on the profile.
    - Facts are deduplicated by Jaccard similarity before persistence.
    """

    EXTRACTION_PROMPT_TEMPLATE = """You are an agricultural user profile extraction system.

Analyze the user's message and extract structured information about the user.

User Message: "{message}"
Detected Language: {language} ({language_name})

Extract and respond ONLY with valid JSON in this exact format:
{{
    "expertise_level": "beginner|intermediate|expert|null",
    "farm_type": "organic|conventional|dairy|arable|mixed|horticulture|vineyard|livestock|null",
    "region": "country or region name (in English) or null",
    "crops": ["crop1", "crop2"],
    "topics": ["pest_control", "soil_health", "irrigation", "machinery", "climate", "regulations", "sustainability", "economics", "animal_health", "crop_management"],
    "facts": [
        {{
            "category": "issue|preference|tool|experience|location|farm_type|crop|topic|goal",
            "text": "clear, concise fact in English",
            "confidence": 0.0-1.0
        }}
    ],
    "memory_notes": [
        "short canonical memory note in English, capturing an EXPLICIT long-term fact or preference the user stated about themselves that does NOT fit the structured fact categories above"
    ],
    "communication_preferences": {{
        "style": "detailed|concise|technical|null",
        "preferred_language": "language code if user mentions preference, else null"
    }}
}}

Important Rules:
1. The user message may be in ANY of the 24 EU languages. Understand the meaning and extract it.
2. Output ALL text fields in English for consistency, regardless of input language.
3. Set null for fields not mentioned or uncertain.
4. Be SPECIFIC in fact texts. Include: what, where, when, quantities if mentioned.
5. Confidence scoring:
   - 0.9-1.0: User explicitly stated this fact ("I have...", "My farm is...")
   - 0.7-0.8: Strongly implied from context
   - 0.5-0.6: Inferred or guessed
6. Categories for facts:
   - issue: Problems, pests, diseases, challenges user is facing
   - preference: User likes, prefers, wants to avoid
   - tool: Equipment, software, methods user uses
   - experience: Years farming, certifications, expertise areas
   - location: Region, climate zone, country
   - farm_type: Type of farming operation
   - crop: Specific crops/livestock user grows/raises
   - topic: Areas of interest for future learning
   - goal: User's objectives and plans
7. Capture communication preferences if user mentions them ("explain simply", "technical details", etc.)
8. `memory_notes` are for long-term memory and are subject to a STRICT bar:
   - Each note must reflect something the user EXPLICITLY stated about themselves in this message. Direct first-person assertions only ("I am vegan", "I'm writing a thesis on X", "skip the preamble in your answers", "my farm is run by my two daughters").
   - Write notes in a compact canonical form suitable for display and reuse later, for example: "Prefers concise answers", "Writing a thesis on soil carbon", "Farm is run by two daughters". Do NOT start notes with "User ...".
   - DO NOT speculate about the user's emotional state, mood, frustration, intentions, motivations, or interests. No inferences, no profiling, no "user may be...", "user is likely...", "user seems to be...".
   - DO NOT summarize what the conversation is about. The notes are about the user as a person, not about the current topic.
   - DO NOT store ephemeral session events like what the user just asked for, how they reacted to a previous answer, or that they requested a format/style for a single turn unless it is clearly a persistent preference.
   - DO NOT create a note that uses hedging words like "may", "might", "possibly", "likely", "perhaps", "seems", "appears", "could be", "indicating".
   - DO NOT invent facts. If the message contains nothing the user explicitly stated about themselves, return an empty array.
   - When in doubt, leave it out. The default state of `memory_notes` is `[]`. Only add when the user has unmistakably told you a stable fact about themselves.

Examples:
Input (German): "Ich habe ein Problem mit Blattläusen auf meinen Tomaten in Bayern"
Output: {{
    "expertise_level": null,
    "farm_type": null,
    "region": "Bavaria, Germany",
    "crops": ["tomatoes"],
    "topics": ["pest_control"],
    "facts": [
        {{
            "category": "issue",
            "text": "User has an aphid infestation on tomato plants",
            "confidence": 0.95
        }},
        {{
            "category": "location",
            "text": "User farms in Bavaria, Germany",
            "confidence": 0.9
        }}
    ],
    "communication_preferences": {{"style": null, "preferred_language": null}}
}}

Input (French): "Je suis agriculteur bio depuis 5 ans en Provence, je cultive des lavandes"
Output: {{
    "expertise_level": "intermediate",
    "farm_type": "organic",
    "region": "Provence, France",
    "crops": ["lavender"],
    "topics": [],
    "facts": [
        {{
            "category": "experience",
            "text": "User has been farming organically for 5 years",
            "confidence": 0.95
        }},
        {{
            "category": "farm_type",
            "text": "User operates an organic farm",
            "confidence": 0.95
        }}
    ],
    "communication_preferences": {{"style": null, "preferred_language": null}}
}}"""

    @classmethod
    async def get_or_create_profile(cls, user_uuid: str, auth_token: str = None) -> UserProfile:
        """Get existing profile or create new one."""
        logger.info(f"get_or_create_profile: user_uuid={user_uuid[:8] if user_uuid else None}..., has_auth={bool(auth_token)}")

        if not auth_token:
            logger.warning("No auth_token provided, returning empty profile")
            return UserProfile(user_uuid=user_uuid)

        auth_header = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"

        profile_data = await cls._fetch_profile(user_uuid, auth_header)
        if profile_data:
            logger.info(f"Loaded profile: expertise={profile_data.get('expertise_level')}, farm={profile_data.get('farm_type')}")
            return UserProfile(
                user_uuid=user_uuid,
                expertise_level=profile_data.get('expertise_level', 'beginner'),
                farm_type=profile_data.get('farm_type'),
                region=profile_data.get('region'),
                preferred_language=profile_data.get('preferred_language', 'en'),
                communication_style=profile_data.get('communication_style', 'detailed'),
                crops=profile_data.get('crops_list', []),
                topics=profile_data.get('common_topics', []),
                query_languages=profile_data.get('query_languages', []),
                total_queries=profile_data.get('total_queries', 0),
                first_seen=profile_data.get('first_seen'),
                last_updated=profile_data.get('last_updated'),
            )

        # Create new profile
        await cls._create_profile(user_uuid, auth_header)
        return UserProfile(user_uuid=user_uuid)

    @classmethod
    async def _fetch_profile(cls, user_uuid: str, auth_header: str) -> Optional[Dict]:
        """Fetch profile from Django backend."""
        if not CHAT_BACKEND_URL or not auth_header:
            return None

        url = f"{CHAT_BACKEND_URL}/chat/user/profile/"
        headers = {"Authorization": auth_header}

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=S.VERIFY_SSL) as client:
                r = await client.get(url, headers=headers)
                logger.debug(f"Profile fetch response: {r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    if data.get('status') == 'success':
                        return data.get('profile')
        except httpx.HTTPError as e:
            logger.warning(f"Failed to fetch profile: {e}")
        return None

    @classmethod
    async def _create_profile(cls, user_uuid: str, auth_header: str) -> bool:
        """Create new profile in Django backend (server creates on first GET)."""
        profile = await cls._fetch_profile(user_uuid, auth_header)
        return profile is not None

    @classmethod
    async def update_profile(cls, user_uuid: str, updates: Dict[str, Any], auth_token: str) -> bool:
        """Update profile with new information."""
        if not CHAT_BACKEND_URL or not auth_token:
            return False

        auth_header = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
        url = f"{CHAT_BACKEND_URL}/chat/user/profile/"
        headers = {"Authorization": auth_header}

        updates['last_updated'] = datetime.utcnow().isoformat()

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=S.VERIFY_SSL) as client:
                r = await client.patch(url, json=updates, headers=headers)
                logger.debug(f"Profile update response: {r.status_code}")
                return r.status_code == 200
        except httpx.HTTPError as e:
            logger.warning(f"Failed to update profile: {e}")
        return False

    @classmethod
    async def add_fact(cls, user_uuid: str, fact: UserFact, auth_token: str) -> bool:
        """Add a fact about the user."""
        if not CHAT_BACKEND_URL or not auth_token:
            return False

        auth_header = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
        url = f"{CHAT_BACKEND_URL}/chat/user/facts/"
        headers = {"Authorization": auth_header}

        payload = {
            "fact_category": fact.category,
            "fact_text": fact.text,
            "confidence_score": fact.confidence,
            "source_session_uuid": fact.session_uuid,
            "source_language": fact.source_language,
            "metadata": {
                "extracted_at": fact.extracted_at,
                "source_query_hash": fact.source_query_hash,
            }
        }

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=S.VERIFY_SSL) as client:
                r = await client.post(url, json=payload, headers=headers)
                logger.debug(f"Add fact response: {r.status_code}")
                return r.status_code in [201, 200]
        except httpx.HTTPError as e:
            logger.warning(f"Failed to add fact: {e}")
        return False

    @classmethod
    async def get_facts(cls, user_uuid: str, auth_token: str, category: Optional[str] = None, limit: int = 10) -> List[Dict]:
        """Get facts about a user."""
        if not CHAT_BACKEND_URL or not auth_token:
            return []

        auth_header = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
        url = f"{CHAT_BACKEND_URL}/chat/user/facts/"
        headers = {"Authorization": auth_header}
        params = {"limit": limit}
        if category:
            params["category"] = category

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=S.VERIFY_SSL) as client:
                r = await client.get(url, params=params, headers=headers)
                logger.debug(f"Get facts response: {r.status_code}")
                if r.status_code == 200:
                    return r.json().get('results', [])
        except httpx.HTTPError as e:
            logger.warning(f"Failed to get facts: {e}")
        return []

    @classmethod
    async def add_memory_note(
        cls,
        user_uuid: str,
        note_text: str,
        auth_token: str,
        confidence: float = 0.85,
        source_language: str = "en",
        session_uuid: Optional[str] = None,
    ) -> bool:
        """
        Add a free-form memory note about the user (ChatGPT-style memory).
        Server side: idempotent on exact-match note_text per-user.
        """
        if not CHAT_BACKEND_URL or not auth_token:
            return False
        text = (note_text or "").strip()
        if not text:
            return False

        auth_header = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
        url = f"{CHAT_BACKEND_URL}/chat/user/memory/"
        headers = {"Authorization": auth_header}
        payload = {
            "note_text": text,
            "confidence_score": float(confidence),
            "source_language": source_language,
        }
        if session_uuid:
            payload["source_session_uuid"] = session_uuid

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=S.VERIFY_SSL) as client:
                r = await client.post(url, json=payload, headers=headers)
                logger.debug(f"Add memory note response: {r.status_code}")
                return r.status_code in (200, 201)
        except httpx.HTTPError as e:
            logger.warning(f"Failed to add memory note: {e}")
        return False

    @classmethod
    async def get_memory_notes(cls, user_uuid: str, auth_token: str, limit: int = 20) -> List[Dict]:
        """List the user's free-form memory notes, newest/highest-confidence first."""
        if not CHAT_BACKEND_URL or not auth_token:
            return []

        auth_header = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
        url = f"{CHAT_BACKEND_URL}/chat/user/memory/"
        headers = {"Authorization": auth_header}
        params = {"limit": limit}

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=S.VERIFY_SSL) as client:
                r = await client.get(url, params=params, headers=headers)
                if r.status_code == 200:
                    return r.json().get("results", [])
        except httpx.HTTPError as e:
            logger.warning(f"Failed to get memory notes: {e}")
        return []

    # Hedging words that indicate the LLM is speculating rather than reporting an
    # explicit user statement. Notes containing these on the property side of the
    # sentence are rejected at the Python layer regardless of the prompt.
    _HEDGE_PATTERNS = (
        " may be ", " may have ", " might be ", " might have ",
        " possibly ", " likely ", " perhaps ", " seems to ", " seems ",
        " appears to ", " appears ", " could be ", " indicating ",
        " suggesting ", " inferred ", " probably ",
    )

    # Memory note will be rejected if the note text mentions any of these — the
    # extraction is supposed to be about the user as a person, not the LLM
    # profiling the user's emotional state.
    _SPECULATIVE_TOPICS = (
        "emotional", "frustration", "frustrated", "distress", "mood",
        "feeling", "feels ", " feel ", "anxious", "anxiety",
        "interested in", "exploring", "seeking clarification",
    )

    _DISALLOWED_NOTE_PREFIXES = (
        "user ",
        "the user ",
        "assistant ",
        "the assistant ",
    )

    _DISALLOWED_NOTE_PATTERNS = (
        "asked about",
        "asking about",
        "requested",
        "requesting",
        "seeking clarification",
        "previous response",
        "previous interaction",
        "previous answer",
        "conversation",
        "chat history",
        "follow-up question",
        "follow up question",
        "tabular format",
        "table format",
        "pretending to",
        "hack",
        "ignore previous instructions",
        "general assistant",
        "question about",
        "wants to talk",
        "doesn't want to talk",
        "don't want to talk",
        "stated '",
        "states '",
    )

    @classmethod
    def _is_memory_note_usable(cls, note_text: str) -> bool:
        """
        Reject turn summaries, adversarial content, hedged speculation, and
        meta commentary. A note that survives every disallow check is treated
        as usable — we do not require a positive keyword match, otherwise
        legitimate notes in any language or phrasing that doesn't happen to
        contain an English signal word get hidden from the user.
        """
        text = (note_text or "").strip()
        if len(text) < 8:
            return False

        lowered = text.lower()
        padded = f" {lowered} "

        if any(lowered.startswith(prefix) for prefix in cls._DISALLOWED_NOTE_PREFIXES):
            return False
        if any(h in padded for h in cls._HEDGE_PATTERNS):
            return False
        if any(topic in lowered for topic in cls._SPECULATIVE_TOPICS):
            return False
        if any(pattern in lowered for pattern in cls._DISALLOWED_NOTE_PATTERNS):
            return False

        # Avoid noisy note shapes like copied quotes or symbol-heavy artifacts.
        alpha_chars = sum(1 for ch in text if ch.isalpha())
        if alpha_chars < max(6, len(text) // 3):
            return False

        return True

    @classmethod
    def _looks_like_explicit_fact(cls, note_text: str) -> bool:
        """
        Reject memory notes that read like LLM speculation rather than an
        explicit user statement. Cheap, language-rough check; works alongside
        the prompt-level instructions.
        """
        return cls._is_memory_note_usable(note_text)

    @classmethod
    def filter_memory_notes(cls, memory_notes: List[Dict], limit: Optional[int] = None) -> List[Dict]:
        """Filter legacy and newly-extracted memory notes down to usable items."""
        filtered: List[Dict] = []
        for note in memory_notes or []:
            text = (note.get("note_text") or "").strip()
            if not cls._is_memory_note_usable(text):
                continue
            filtered.append(note)
            if limit is not None and len(filtered) >= limit:
                break
        return filtered

    @classmethod
    async def delete_memory_note(cls, note_id: int, auth_token: str) -> bool:
        """Soft-delete a single memory note by id."""
        if not CHAT_BACKEND_URL or not auth_token:
            return False
        auth_header = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
        url = f"{CHAT_BACKEND_URL}/chat/user/memory/{int(note_id)}/"
        headers = {"Authorization": auth_header}
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=S.VERIFY_SSL) as client:
                r = await client.delete(url, headers=headers)
                return r.status_code in (200, 204)
        except httpx.HTTPError as e:
            logger.warning(f"Failed to delete memory note: {e}")
        return False

    @classmethod
    async def analyze_message_multilingual(cls, message: str) -> ExtractedProfile:
        """
        Analyze a user message using LLM-based extraction.
        Works with any of the 24 EU languages. Falls back to keyword extraction
        (English-only) if the LLM is unavailable.
        """
        if not message or not message.strip():
            return ExtractedProfile()

        detected_lang = detect_language(message)
        language_name = get_language_name(detected_lang)

        logger.info(f"Analyzing message in {language_name} ({detected_lang}): '{message[:60]}...'")

        if VLLM_AVAILABLE:
            try:
                result = await cls._extract_with_llm(message, detected_lang, language_name)
                if result:
                    result.detected_language = detected_lang
                    return result
            except Exception as e:
                logger.warning(f"LLM extraction failed: {e}")

        logger.info("Falling back to keyword extraction")
        result = cls._extract_with_keywords(message)
        result.detected_language = detected_lang
        return result

    @classmethod
    async def _extract_with_llm(cls, message: str, lang_code: str, lang_name: str) -> Optional[ExtractedProfile]:
        """Use the LLM for intelligent, multilingual extraction."""
        prompt = cls.EXTRACTION_PROMPT_TEMPLATE.format(
            message=message.replace('"', '\\"'),
            language=lang_code,
            language_name=lang_name
        )

        response = await generate_once(
            prompt,
            temperature=0.1,
            max_tokens=800,
        )

        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if not json_match:
                logger.warning("No JSON found in LLM response")
                return None

            data = json.loads(json_match.group())
            if not isinstance(data, dict):
                raise ValueError("Response is not a JSON object")

            facts = []
            for f in data.get('facts', []):
                if isinstance(f, dict) and 'text' in f:
                    facts.append(UserFact(
                        category=f.get('category', 'preference'),
                        text=f['text'],
                        confidence=f.get('confidence', 0.7),
                        source_language=lang_code,
                    ))

            comm_prefs = data.get('communication_preferences', {}) or {}

            memory_notes = [
                n.strip()
                for n in (data.get('memory_notes') or [])
                if isinstance(n, str) and n.strip()
            ]

            return ExtractedProfile(
                expertise_level=data.get('expertise_level'),
                farm_type=data.get('farm_type'),
                region=data.get('region'),
                crops=[c.lower() for c in (data.get('crops') or []) if c],
                topics=data.get('topics') or [],
                facts=facts,
                memory_notes=memory_notes,
                communication_style=comm_prefs.get('style'),
                preferred_language=comm_prefs.get('preferred_language'),
                detected_language=lang_code,
            )

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM JSON response: {e}")
            return None
        except Exception as e:
            logger.warning(f"Error processing LLM response: {e}")
            return None

    @classmethod
    def _extract_with_keywords(cls, message: str) -> ExtractedProfile:
        """English-only keyword fallback used when the LLM is unavailable."""
        message_lower = message.lower()
        result = ExtractedProfile()

        expertise_keywords = {
            'beginner': ['new to', 'just started', 'beginner', 'learning', 'novice', 'first time', 'getting started'],
            'expert': ['experienced', 'expert', 'advanced', 'specialist', 'professional', 'many years', 'deep knowledge'],
        }
        for level, keywords in expertise_keywords.items():
            if any(kw in message_lower for kw in keywords):
                result.expertise_level = level
                break

        farm_type_keywords = {
            'organic': ['organic', 'bio', 'natural farming', 'no chemicals'],
            'conventional': ['conventional', 'traditional', 'standard'],
            'dairy': ['dairy', 'milk', 'cows', 'cattle', 'livestock'],
            'arable': ['arable', 'crops', 'grain', 'cereals'],
            'mixed': ['mixed farm', 'diverse', 'multiple crops'],
            'horticulture': ['horticulture', 'vegetables', 'fruits', 'greenhouse'],
            'vineyard': ['vineyard', 'wine', 'grapes', 'viticulture'],
        }
        for ftype, keywords in farm_type_keywords.items():
            if any(kw in message_lower for kw in keywords):
                result.farm_type = ftype
                break

        crop_keywords = [
            'wheat', 'barley', 'corn', 'maize', 'rice', 'oats', 'rye',
            'potato', 'potatoes', 'tomato', 'tomatoes', 'onion', 'onions',
            'apple', 'apples', 'grape', 'grapes', 'olive', 'olives',
            'cow', 'cows', 'cattle', 'sheep', 'pig', 'pigs', 'chicken', 'chickens'
        ]
        result.crops = [c for c in crop_keywords if c in message_lower]

        region_patterns = [
            r'\b(?:in|from|located in)\s+([a-z][a-z\s\-]{1,50})\b',
        ]
        for pattern in region_patterns:
            match = re.search(pattern, message_lower, re.IGNORECASE)
            if match:
                potential_region = match.group(1).strip(" ,.")
                if potential_region not in {'the', 'my', 'our', 'this', 'that'}:
                    result.region = potential_region.title()
                    break

        return result

    @classmethod
    def is_duplicate_fact(cls, new_fact: UserFact, existing_facts: List[Dict], threshold: float = 0.85) -> bool:
        """
        Check if a fact is semantically similar to existing facts via Jaccard
        similarity over word sets. Cheap; for tighter dedup, swap in an
        embedding-based check later.
        """
        new_text = new_fact.text.lower()
        new_words = set(re.findall(r'\b\w+\b', new_text))

        for existing in existing_facts:
            existing_text = existing.get('text', '').lower()
            existing_words = set(re.findall(r'\b\w+\b', existing_text))

            if not new_words or not existing_words:
                continue

            intersection = new_words & existing_words
            union = new_words | existing_words
            if union:
                similarity = len(intersection) / len(union)
                if similarity >= threshold:
                    logger.debug(f"Duplicate detected (sim={similarity:.2f}): '{new_fact.text}' vs '{existing_text}'")
                    return True

        return False

    @classmethod
    async def process_conversation_turn(
        cls,
        user_uuid: str,
        session_uuid: str,
        user_message: str,
        assistant_message: str,
        auth_token: str
    ) -> Dict[str, Any]:
        """
        Process a conversation turn to update the user profile.
        Multilingual extraction + deduplication; safe to call as fire-and-forget.
        """
        result = {
            "profile_updated": False,
            "facts_added": 0,
            "language_detected": "en",
            "errors": []
        }

        if not auth_token:
            result["errors"].append("No auth token provided")
            return result

        try:
            extracted = await cls.analyze_message_multilingual(user_message)
            result["language_detected"] = extracted.detected_language

            logger.info(
                f"Extracted from {extracted.detected_language}: "
                f"expertise={extracted.expertise_level}, "
                f"farm={extracted.farm_type}, "
                f"crops={extracted.crops}, "
                f"facts={len(extracted.facts)}"
            )

            profile = await cls.get_or_create_profile(user_uuid, auth_token)

            updates: Dict[str, Any] = {}

            if extracted.expertise_level and extracted.expertise_level != profile.expertise_level:
                updates['expertise_level'] = extracted.expertise_level

            if extracted.farm_type and extracted.farm_type != profile.farm_type:
                updates['farm_type'] = extracted.farm_type

            if extracted.region and extracted.region != profile.region:
                updates['region'] = extracted.region

            new_crops = [c for c in extracted.crops if c not in profile.crops]
            if new_crops:
                updates['crops_list'] = profile.crops + new_crops

            new_topics = [t for t in extracted.topics if t not in profile.topics]
            if new_topics:
                updates['common_topics'] = profile.topics + new_topics

            if extracted.communication_style and extracted.communication_style != profile.communication_style:
                updates['communication_style'] = extracted.communication_style

            if extracted.preferred_language and extracted.preferred_language != profile.preferred_language:
                updates['preferred_language'] = extracted.preferred_language

            query_langs = set(profile.query_languages)
            query_langs.add(extracted.detected_language)
            if query_langs != set(profile.query_languages):
                updates['query_languages'] = list(query_langs)

            updates['total_queries'] = profile.total_queries + 1

            if updates:
                logger.info(f"Updating profile with: {list(updates.keys())}")
                success = await cls.update_profile(user_uuid, updates, auth_token)
                result["profile_updated"] = success

            existing_facts = await cls.get_facts(user_uuid, auth_token, limit=50)

            facts_added = 0
            for fact in extracted.facts:
                fact.session_uuid = session_uuid
                fact.source_query_hash = hashlib.sha256(user_message.encode()).hexdigest()[:16]
                fact.extracted_at = datetime.utcnow().isoformat()

                if not cls.is_duplicate_fact(fact, existing_facts):
                    success = await cls.add_fact(user_uuid, fact, auth_token)
                    if success:
                        facts_added += 1
                        logger.info(f"Added fact: {fact.text[:60]}...")
                else:
                    logger.debug(f"Skipped duplicate fact: {fact.text[:50]}...")

            result["facts_added"] = facts_added

            # Free-form memory notes (ChatGPT-style memory channel).
            # Three-stage gate: prompt-level rule, hedging-word filter, semantic-dedup
            # against existing notes. Plus a hard cap so memory can't grow forever.
            MEMORY_NOTE_CAP = 30
            DEDUP_THRESHOLD = 0.55  # Lower than fact dedup; more aggressive merging.

            existing_notes = cls.filter_memory_notes(
                await cls.get_memory_notes(user_uuid, auth_token, limit=MEMORY_NOTE_CAP),
                limit=MEMORY_NOTE_CAP,
            )
            existing_note_dicts = [{"text": n.get("note_text", "")} for n in existing_notes]
            at_cap = len(existing_notes) >= MEMORY_NOTE_CAP
            notes_added = 0
            notes_rejected_speculative = 0

            for note_text in extracted.memory_notes:
                if not cls._looks_like_explicit_fact(note_text):
                    notes_rejected_speculative += 1
                    logger.info(f"Rejected speculative memory note: {note_text[:80]}...")
                    continue

                pseudo_fact = UserFact(category="note", text=note_text)
                if cls.is_duplicate_fact(pseudo_fact, existing_note_dicts, threshold=DEDUP_THRESHOLD):
                    logger.debug(f"Skipped duplicate-or-similar memory note: {note_text[:50]}...")
                    continue

                if at_cap:
                    # User is at the soft cap; refuse new notes rather than evict at
                    # random. The user can prune via the memory UI when they want
                    # the model to remember something else.
                    logger.info(f"Memory at cap ({MEMORY_NOTE_CAP}); skipped: {note_text[:60]}...")
                    continue

                success = await cls.add_memory_note(
                    user_uuid=user_uuid,
                    note_text=note_text,
                    auth_token=auth_token,
                    confidence=0.9,
                    source_language=extracted.detected_language,
                    session_uuid=session_uuid,
                )
                if success:
                    notes_added += 1
                    # Append to in-memory list so subsequent notes in the same batch
                    # also dedup against just-added ones.
                    existing_note_dicts.append({"text": note_text})
                    logger.info(f"Added memory note: {note_text[:60]}...")

            result["memory_notes_added"] = notes_added
            if notes_rejected_speculative:
                result["memory_notes_rejected"] = notes_rejected_speculative

        except Exception as e:
            logger.error(f"Error processing conversation turn: {e}", exc_info=True)
            result["errors"].append(str(e))

        return result

    @classmethod
    def build_profile_context(
        cls,
        profile: UserProfile,
        facts: List[Dict] = None,
        memory_notes: List[Dict] = None,
    ) -> str:
        """
        Build a bullet list of background facts + free-form memory notes about
        the user. Returns just the bulleted body — the prompt assembler is
        responsible for framing it ("background you may know"), gating when
        it's included, and instructing the model not to surface these details
        on unrelated turns.
        """
        context_parts: List[str] = []

        if profile.expertise_level:
            context_parts.append(f"Expertise level: {profile.expertise_level}")

        if profile.farm_type:
            context_parts.append(f"Farm type: {profile.farm_type}")

        if profile.region:
            context_parts.append(f"Region: {profile.region}")

        if profile.crops:
            crops_str = ', '.join(profile.crops[:5])
            context_parts.append(f"Crops/livestock: {crops_str}")

        if profile.communication_style == 'concise':
            context_parts.append("Prefers concise answers")
        elif profile.communication_style == 'technical':
            context_parts.append("Prefers technical, detailed information")

        if profile.preferred_language and profile.preferred_language != 'en':
            context_parts.append(f"Prefers responses in {get_language_name(profile.preferred_language)}")

        if profile.total_queries > 5:
            context_parts.append(f"Returning user ({profile.total_queries} prior queries)")

        if facts:
            important_facts = [f for f in facts if f.get('confidence', 0) > 0.7][:3]
            for fact in important_facts:
                context_parts.append(f"Fact: {fact['text']}")

        if memory_notes:
            # Free-form memory notes — keep the top few by confidence.
            important_notes = [
                n
                for n in cls.filter_memory_notes(memory_notes, limit=5)
                if float(n.get('confidence', 0) or 0) > 0.6
            ]
            for note in important_notes:
                text = (note.get('note_text') or '').strip()
                if text:
                    context_parts.append(f"Remembered: {text}")

        if context_parts:
            return "\n".join(f"- {part}" for part in context_parts)

        return ""
