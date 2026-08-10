"""
qa_resolver.py — Four-tier adaptive Q&A resolver for Easy Apply form fields.
Tiers: qa_bank exact match -> qa_bank fuzzy match -> config-keyword match ->
Haiku AI fallback with confidence gating. Imported by applicant.py only —
must never import from applicant.py (no circular deps).
"""
import difflib
import json
from dataclasses import dataclass

import anthropic

import config
import db
from utils import setup_logger


@dataclass
class ResolvedField:
    label: str
    field_type: str
    options: list[str] | None
    answer: str | None           # None means unresolved
    source: str                  # 'bank_exact' | 'bank_fuzzy' | 'config_rule' | 'ai' | 'unresolved'
    ai_suggestion: str | None = None   # populated on tier-4 even if below threshold
    ai_confidence: float | None = None

    @property
    def resolved(self) -> bool:
        return self.answer is not None


class QAResolver:

    def __init__(self):
        self.logger = setup_logger("qa_resolver")
        self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # Load bank once per run. Rows saved during this run appear here
        # only on the NEXT run — cache staleness within a run is intentional
        # (acceptable for a personal tool; do not add reload logic).
        self._bank_cache: list = db.get_qa_bank_all()
        self.logger.info(f"QA bank loaded: {len(self._bank_cache)} entries")

        self._profile_summary = self._build_profile_summary()

    # ------------------------------------------------------------------
    # Profile summary for the AI prompt
    # ------------------------------------------------------------------

    def _build_profile_summary(self) -> str:
        """
        Compact candidate profile for tier-4 AI prompts.
        Loads master.json fresh. Falls back to config values only if
        master.json is missing or malformed — never raises.
        Target: under 200 tokens.
        """
        from pathlib import Path

        skills_str = "Not specified"
        summary_str = "Not specified"

        try:
            master_path = Path(config.RESUME_MASTER_PATH)
            if master_path.exists():
                with open(master_path, encoding='utf-8') as f:
                    master = json.load(f)
                summary_str = master.get('summary', '')[:200]
                skills = master.get('skills', {})
                all_skills = (
                    skills.get('languages', []) +
                    skills.get('frameworks', []) +
                    skills.get('tools', [])
                )
                skills_str = ', '.join(all_skills[:15])  # cap at 15 to keep tokens low
        except Exception as e:
            self.logger.warning(f"Could not load master.json for profile summary: {e}")

        return (
            f"Summary: {summary_str}\n"
            f"Skills: {skills_str}\n"
            f"Years of experience: {config.EXPERIENCE_YEARS}\n"
            f"Current CTC: {config.CURRENT_CTC}\n"
            f"Expected CTC: {config.EXPECTED_CTC}\n"
            f"Notice period (days): {config.NOTICE_PERIOD_DAYS}\n"
            f"Phone: {config.PHONE_NUMBER}"
        )

    # ------------------------------------------------------------------
    # Tier 1 + 2: bank matching
    # ------------------------------------------------------------------

    @staticmethod
    def normalize(text: str) -> str:
        """Delegates to db.normalize_question — single source of truth."""
        return db.normalize_question(text)

    def _fuzzy_match(self, question_norm: str, field_type: str):
        """
        Runs difflib.SequenceMatcher over self._bank_cache filtered to
        the same field_type. Returns (entry, ratio) for the best match
        if ratio >= config.QA_FUZZY_MATCH_THRESHOLD, otherwise None.
        Filtering by field_type first dramatically reduces comparisons.
        """
        same_type = [e for e in self._bank_cache if e.field_type == field_type]
        if not same_type:
            return None

        best_entry = None
        best_ratio = 0.0
        for entry in same_type:
            ratio = difflib.SequenceMatcher(
                None, question_norm, entry.question_norm
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_entry = entry

        if best_ratio >= config.QA_FUZZY_MATCH_THRESHOLD and best_entry:
            return best_entry, best_ratio
        return None

    # ------------------------------------------------------------------
    # Tier 3: config-keyword match (relocated from fill_form_fields)
    # ------------------------------------------------------------------

    def _legacy_config_match(
        self,
        label: str,
        field_type: str,
        options: list[str] | None,
    ) -> str | None:
        """
        Ports the keyword-matching logic that used to live inline in
        fill_form_fields() (Current CTC / Expected CTC / Notice period /
        Years of experience / phone / work-authorization blocks, plus the
        standalone work-auth radio fallback) — same answer values, same
        precedence. Keyword lists are widened beyond the original single
        phrase per field to cover every phrasing the old code actually
        matched against (e.g. "desired salary", "joining", "total years",
        "eligible to work", "legally authorized", "citizen").

        Returns the answer string if a rule matches, or None.
        """
        label_lower = label.lower()

        # Salary / CTC questions
        if any(kw in label_lower for kw in ['current ctc', 'current salary', 'current compensation']):
            return str(config.CURRENT_CTC)

        if any(kw in label_lower for kw in
               ['expected ctc', 'expected salary', 'expected compensation', 'desired salary']):
            return str(config.EXPECTED_CTC)

        # Notice period
        if any(kw in label_lower for kw in ['notice period', 'notice', 'joining']):
            return str(config.NOTICE_PERIOD_DAYS)

        # Years of experience
        if any(kw in label_lower for kw in [
            'years of experience', 'experience in years', 'years experience',
            'years of professional experience', 'total years',
        ]):
            return str(config.EXPERIENCE_YEARS)

        # Phone
        if any(kw in label_lower for kw in ['phone', 'mobile', 'contact number']):
            return str(config.PHONE_NUMBER)

        # Work authorization / visa
        if any(kw in label_lower for kw in [
            'work authorization', 'authorized to work', 'visa', 'work permit',
            'authorized', 'eligible to work', 'legally authorized', 'citizen',
        ]):
            if options:
                for opt in options:
                    opt_lower = opt.lower()
                    if any(w in opt_lower for w in ['yes', 'authorized', 'citizen', 'legally eligible']):
                        return opt
            return 'Yes'

        return None

    # ------------------------------------------------------------------
    # Tier 4: Haiku AI fallback
    # ------------------------------------------------------------------

    def _build_ai_prompt(
        self, label: str, field_type: str, options: list[str] | None
    ) -> str:
        return f"""You are helping fill out a job application form question. Follow these rules strictly:

RULES:
1. NEVER invent or fabricate specific facts, employers, dates, or achievements not in the profile below
2. If the question needs a specific fact not in the profile, return answer: null and confidence: 0
3. For general willingness questions (relocation, remote, notice period, work authorization, shift work), reason from context only when low-risk
4. If field_type is "radio" or "select": answer MUST be copied EXACTLY from the options list — never invent an option
5. If field_type is "number": digits only — no currency symbols, commas, or units
6. Keep any free-text answer under 40 words
7. Return ONLY valid JSON — no markdown fences, no preamble, no explanation

CANDIDATE PROFILE:
{self._profile_summary}

QUESTION:
Label: {label}
Field type: {field_type}
Options (if any): {options}

Respond with ONLY this JSON object:
{{
  "answer": <string matching an option exactly, or a short string, or null>,
  "confidence": <float 0.0-1.0>,
  "reasoning": "<one short sentence>"
}}"""

    async def _ask_ai(
        self, label: str, field_type: str, options: list[str] | None
    ) -> tuple[str | None, float, str]:
        """
        Calls claude-haiku-4-5. max_tokens=300 (higher than scorer — reasoning
        field needs room). Strips ```json fences before parsing.
        Returns (answer, confidence, reasoning).
        On ANY error (API error, parse error, invalid JSON): returns
        (None, 0.0, "error") — never raises, never crashes the batch.
        """
        try:
            response = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                messages=[{"role": "user", "content": self._build_ai_prompt(label, field_type, options)}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown code fences (Haiku sometimes adds them despite instructions)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            data = json.loads(raw)
            return (
                data.get("answer"),
                float(data.get("confidence", 0.0)),
                data.get("reasoning", ""),
            )
        except Exception as e:
            self.logger.warning(f"AI tier failed for '{label}': {e}")
            return (None, 0.0, "error")

    # ------------------------------------------------------------------
    # Main resolution entry point
    # ------------------------------------------------------------------

    async def resolve_field(
        self,
        label: str,
        field_type: str,
        options: list[str] | None = None,
    ) -> ResolvedField:
        """
        Runs the four-tier cascade for one form field.
        Returns a ResolvedField. Check .resolved to know if an answer was found.
        """
        question_norm = self.normalize(label)

        # Tier 1: exact bank match
        exact = next(
            (e for e in self._bank_cache
             if e.question_norm == question_norm and e.field_type == field_type),
            None
        )
        if exact:
            db.increment_qa_use_count(exact.id)
            self.logger.info(f"  Tier 1 (bank_exact): '{label}' -> '{exact.answer}'")
            return ResolvedField(label, field_type, options, exact.answer, "bank_exact")

        # Tier 2: fuzzy bank match
        fuzzy = self._fuzzy_match(question_norm, field_type)
        if fuzzy:
            entry, ratio = fuzzy
            db.increment_qa_use_count(entry.id)
            self.logger.info(f"  Tier 2 (bank_fuzzy, {ratio:.2f}): '{label}' -> '{entry.answer}'")
            return ResolvedField(label, field_type, options, entry.answer, "bank_fuzzy",
                                 ai_confidence=ratio)

        # Tier 3: config-keyword match
        legacy = self._legacy_config_match(label, field_type, options)
        if legacy is not None:
            # Save to bank so next occurrence is tier-1
            db.upsert_qa_answer(
                question_norm, label, field_type,
                json.dumps(options) if options else None,
                legacy, "config", None,
            )
            self.logger.info(f"  Tier 3 (config_rule): '{label}' -> '{legacy}'")
            return ResolvedField(label, field_type, options, legacy, "config_rule")

        # Tier 4: Haiku AI
        ai_answer, ai_confidence, reasoning = await self._ask_ai(label, field_type, options)
        self.logger.info(
            f"  Tier 4 (ai, conf={ai_confidence:.2f}): '{label}' -> '{ai_answer}' | {reasoning}"
        )
        if ai_answer is not None and ai_confidence >= config.QA_AI_CONFIDENCE_THRESHOLD:
            db.upsert_qa_answer(
                question_norm, label, field_type,
                json.dumps(options) if options else None,
                ai_answer, "ai", ai_confidence,
            )
            return ResolvedField(label, field_type, options, ai_answer, "ai",
                                 ai_confidence=ai_confidence)

        # Unresolved — return AI suggestion for the review UI even if below threshold
        self.logger.warning(
            f"  Unresolved: '{label}' (ai_conf={ai_confidence:.2f} < threshold "
            f"{config.QA_AI_CONFIDENCE_THRESHOLD})"
        )
        return ResolvedField(
            label, field_type, options, None, "unresolved",
            ai_suggestion=ai_answer, ai_confidence=ai_confidence,
        )
