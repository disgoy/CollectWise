// The language a live call is held in (M7.6): chosen before the call, sent
// when the conversation starts, and fixed for it. The server decides what
// each language means (prompt, listener, voice); the page only asks.

export const LANGUAGES = Object.freeze({ en: "English", hi: "हिंदी" });

// The start-conversation request body: English is the server's default, so
// it sends nothing extra; an unknown choice falls back to English.
export function conversationBody(language) {
  return Object.hasOwn(LANGUAGES, language) && language !== "en" ? { language } : {};
}

// What the status line says the call is in; nothing for English.
export function callLabel(language) {
  return language === "hi" ? `${LANGUAGES.hi} call` : "";
}
