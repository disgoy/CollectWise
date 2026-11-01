import assert from "node:assert/strict";
import { test } from "node:test";

import { LANGUAGES, callLabel, conversationBody } from "../../app/static/live/language.mjs";

test("the two languages on offer", () => {
  assert.deepEqual(Object.keys(LANGUAGES), ["en", "hi"]);
  assert.equal(LANGUAGES.hi, "हिंदी");
});

test("only Hindi is sent; English is the server's default", () => {
  assert.deepEqual(conversationBody("hi"), { language: "hi" });
  assert.deepEqual(conversationBody("en"), {});
  assert.deepEqual(conversationBody("fr"), {});
  assert.deepEqual(conversationBody("__proto__"), {});
});

test("a Hindi call says so; an English one says nothing extra", () => {
  assert.equal(callLabel("hi"), "हिंदी call");
  assert.equal(callLabel("en"), "");
});
