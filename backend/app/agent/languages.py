"""The languages a conversation can be held in (M7.6): English, and Hindi as
people speak it (Hinglish). Chosen when the conversation starts and fixed for
it: the system prompt and every fixed reply follow it. The rules don't
change with the language: the Hindi prompt is the English one, word for
word, plus how to speak.

Fixed replies promise nothing and state no account facts. A handoff is
recorded for the team, not a live transfer (M7.3 is deferred), so none says
"connecting you" or "hold". The Hindi ones are in the agent's voice, Rahul,
so they speak as a man ("नहीं पाया").
"""

from dataclasses import dataclass, fields
from typing import Literal

from backend.app.agent.prompts import SYSTEM_PROMPT

LanguageCode = Literal["en", "hi"]


@dataclass(frozen=True)
class Replies:
    # The agent loop: the model gave no usable answer.
    cannot_help: str
    unavailable: str
    stuck: str
    empty: str
    handed_off: str
    turn_limit: str
    # The guard: words that never reach the model.
    not_understood: str
    too_long: str
    cant_hear: str
    # Live calls ending or pausing.
    goodbye_cant_hear: str
    time_limit: str
    silence: str
    slow_down: str


@dataclass(frozen=True)
class Language:
    code: LanguageCode
    system_prompt: str
    replies: Replies


ENGLISH = Language(
    code="en",
    system_prompt=SYSTEM_PROMPT,
    replies=Replies(
        cannot_help="Sorry, I can't help with that here. Is there anything else I can do for you?",
        unavailable="Sorry, I'm having trouble right now. Please try again in a moment.",
        stuck="Sorry, I wasn't able to finish that. I've passed your conversation to a member of our team.",
        empty="Sorry, could you say that again?",
        handed_off="Your conversation has been passed to a member of our team.",
        turn_limit="We've covered a lot in this call, so I've passed your conversation to a member of our team.",
        not_understood="Sorry, I didn't catch that. Could you say it again?",
        too_long="Sorry, that was a lot at once. Could you say it more briefly?",
        cant_hear="Sorry, I'm having trouble hearing right now. Please try again in a moment.",
        goodbye_cant_hear="Sorry, I'm having trouble hearing you, so I'll have to end this call. Please try again later.",
        time_limit="We've reached the time limit for this call, so I'll end it here. Thank you for calling.",
        silence="I haven't heard anything for a while, so I'll end this call. Please call again any time.",
        slow_down="Sorry, I need a moment to catch up. Please say that again in a few seconds.",
    ),
)

HINDI_SECTION = """
Language:
- The customer chose Hindi. Reply in Hindi the way they speak it: Hindi words \
in Devanagari script, and the English words they use (account, payment, \
savings, balance) in Latin letters.
- You speak as a man (for example "मैं कर सकता हूँ", "मैं समझ नहीं पाया").
- Write every amount in digits with its currency, never in words: for example \
"2,000.00 रुपये" for INR, or "120.00 US dollars".
- Call accounts by their labels from tool results, as written there.
- If the customer switches to English, you may reply in English.
- Every rule above applies unchanged in any language.
"""

HINDI = Language(
    code="hi",
    system_prompt=SYSTEM_PROMPT + HINDI_SECTION,
    replies=Replies(
        cannot_help="माफ़ कीजिए, मैं यहाँ इसमें मदद नहीं कर सकता। क्या मैं आपकी किसी और चीज़ में मदद कर सकता हूँ?",
        unavailable="माफ़ कीजिए, अभी मुझे कुछ दिक्कत हो रही है। कृपया थोड़ी देर में फिर कोशिश कीजिए।",
        stuck="माफ़ कीजिए, मैं यह पूरा नहीं कर पाया। मैंने आपकी बातचीत हमारी team के एक सदस्य को भेज दी है।",
        empty="माफ़ कीजिए, क्या आप यह फिर से बोल सकते हैं?",
        handed_off="आपकी बातचीत हमारी team के एक सदस्य को भेज दी गई है।",
        turn_limit="हमने इस call में काफ़ी बातें कर ली हैं, इसलिए मैंने आपकी बातचीत हमारी team के एक सदस्य को भेज दी है।",
        not_understood="माफ़ कीजिए, मैं ठीक से सुन नहीं पाया। क्या आप फिर से बोल सकते हैं?",
        too_long="माफ़ कीजिए, यह एक साथ बहुत ज़्यादा था। क्या आप थोड़ा छोटा करके बता सकते हैं?",
        cant_hear="माफ़ कीजिए, अभी मुझे सुनने में दिक्कत हो रही है। कृपया थोड़ी देर में फिर कोशिश कीजिए।",
        goodbye_cant_hear="माफ़ कीजिए, मुझे आपको सुनने में दिक्कत हो रही है, इसलिए मुझे यह call खत्म करनी होगी। "
                          "कृपया बाद में फिर कोशिश कीजिए।",
        time_limit="इस call का समय पूरा हो गया है, इसलिए मैं इसे यहीं खत्म कर रहा हूँ। Call करने के लिए धन्यवाद।",
        silence="काफ़ी देर से मुझे कुछ सुनाई नहीं दिया, इसलिए मैं यह call खत्म कर रहा हूँ। आप कभी भी फिर से call कर सकते हैं।",
        slow_down="माफ़ कीजिए, मुझे एक पल चाहिए। कृपया कुछ सेकंड बाद फिर से बोलिए।",
    ),
)

LANGUAGES: dict[str, Language] = {language.code: language for language in (ENGLISH, HINDI)}
REPLY_NAMES = tuple(field.name for field in fields(Replies))


def language(code: str) -> Language:
    """The language for a code; raises ValueError for any other."""
    try:
        return LANGUAGES[code]
    except KeyError:
        raise ValueError(f"unknown language {code!r}") from None
