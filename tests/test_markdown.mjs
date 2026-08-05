// Tests the markdown renderer by extracting it from the shipped index.html,
// so what is asserted is exactly what the browser gets.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// The renderer is extracted from the shipped page, so what is asserted here is
// exactly what a browser gets — there is no second copy to drift.
const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
  join(here, "..", "src", "nati", "web", "index.html"), "utf8");
const start = src.indexOf("const md = (() => {");
const end = src.indexOf("// --- render: turn state into DOM");
if (start < 0 || end < 0) { console.error("could not locate the md module"); process.exit(1); }
const md = new Function(src.slice(start, end) + "\nreturn md;")();

const lines = [];
let failed = 0;
const t = (label, input, checks) => {
  let html;
  try { html = md.render(input); }
  catch (e) { lines.push(`FAIL ${label}: threw ${e.message}`); failed++; return; }
  const bad = checks.filter(([, fn]) => !fn(html)).map(([n]) => n);
  if (bad.length) {
    failed++;
    lines.push(`FAIL ${label}\n      failed: ${bad.join(", ")}\n      got: ${html}`);
  } else {
    lines.push(`ok   ${label}`);
  }
};
const has = (s) => ["has " + JSON.stringify(s), (h) => h.includes(s)];
const lacks = (s) => ["lacks " + JSON.stringify(s), (h) => !h.includes(s)];

console.log("--- XSS / escaping (the ones that matter in a red-team target) ---");
t("script tag is inert", "<script>alert(1)</script>",
  [lacks("<script"), has("&lt;script")]);
t("img onerror is inert", '<img src=x onerror="alert(1)">',
  [lacks("<img"), lacks('onerror="alert')]);
t("bold cannot smuggle a tag", "**<b>hi</b>**",
  [has("<strong>"), lacks("<b>hi")]);
t("attribute break-out escaped", '`" onmouseover="alert(1)`',
  [lacks('onmouseover="alert')]);
t("javascript: link is not an anchor", "[click](javascript:alert(1))",
  [lacks("<a "), lacks("href")]);
t("http link is not an anchor either", "[docs](https://example.com)",
  [lacks("<a "), has("https://example.com")]);
t("forged sentinel cannot escape code", " 0  **x**",
  [has("<strong>x</strong>"), lacks("<code>")]);
t("svg onload is inert", "<svg onload=alert(1)>", [lacks("<svg")]);
t("iframe is inert", '<iframe src="evil"></iframe>', [lacks("<iframe")]);
t("table cell cannot smuggle a tag", "| a |\n|---|\n| <img src=x> |",
  [lacks("<img")]);
t("code fence cannot smuggle a tag", "```\n<script>alert(1)</script>\n```",
  [lacks("<script"), has("&lt;script")]);
t("heading cannot smuggle a tag", "# <script>alert(1)</script>",
  [lacks("<script")]);

console.log("--- formatting ---");
t("bold", "The fund has **$15,732** in cash.", [has("<strong>$15,732</strong>")]);
t("italic star", "that is *her* money", [has("<em>her</em>")]);
t("italic underscore", "that is _her_ money", [has("<em>her</em>")]);
t("bold+italic", "***both***", [has("<strong><em>both</em></strong>")]);
t("bold underscore", "__strong__", [has("<strong>strong</strong>")]);
t("strikethrough", "~~void~~", [has("<del>void</del>")]);
t("snake_case is not italic", "the member_id and loan_id fields",
  [lacks("<em>"), has("member_id")]);
t("headings", "# One\n## Two\n### Three",
  [has("<h1>One</h1>"), has("<h2>Two</h2>"), has("<h3>Three</h3>")]);
t("inline code", "call `fund_status` now", [has("<code>fund_status</code>")]);
t("emphasis inside code stays literal", "`a**b**c`",
  [has("<code>a**b**c</code>"), lacks("<strong>")]);
t("fenced code", '```json\n{"a": **1**}\n```',
  [has("<pre><code>"), lacks("<strong>")]);
t("bullets", "- one\n- two", [has("<ul>"), has("<li>one</li>")]);
t("numbered", "1. first\n2. second", [has("<ol>"), has("<li>first</li>")]);
t("ordered start attr", "3. third", [has('<ol start="3">')]);
t("nested list", "- outer\n  - inner",
  [["nested ul inside li", (h) => /<li>outer<ul>/.test(h)]]);
t("blockquote", "> quoted", [has("<blockquote>"), has("quoted")]);
t("hr", "---", [has("<hr>")]);
t("paragraph", "Hello there.", [has("<p>Hello there.</p>")]);
t("two paragraphs", "One.\n\nTwo.",
  [["exactly two <p>", (h) => (h.match(/<p>/g) || []).length === 2]]);
t("single newline is a break", "Line one\nLine two", [has("<br>")]);
t("table + alignment", "| Member | Saved |\n|---|---:|\n| Carlos | $610 |",
  [has("<table>"), has("<th"), has("text-align:right"), has("Carlos")]);
t("table is scrollable", "| a |\n|---|\n| b |", [has('class="tablewrap"')]);

console.log("--- robustness ---");
t("empty string", "", [["no throw", () => true]]);
t("null", null, [["no throw", () => true]]);
t("unclosed fence", "```\nstuck", [has("<pre>")]);
t("unclosed bold stays literal", "**dangling", [has("**dangling")]);
t("lone asterisk in arithmetic", "5 * 3 = 15", [lacks("<em>")]);
t("dollar amounts survive", "$15,732 and $2,121", [has("$15,732")]);
t("a real Qwen reply",
  "**Loan Details**\n- **Amount**: $500 to Carlos Ramirez (Member #2)\n" +
  "- **Interest**: 3% monthly on the original amount\n\nConfirm?",
  [has("<strong>Loan Details</strong>"), has("<ul>"),
   has("<strong>Amount</strong>"), has("<p>Confirm?</p>")]);
t("the blocked-reply sentence is untouched",
  "I wrote an answer that included figures I could not verify against the " +
  "books, so I have not sent it.",
  [has("<p>I wrote an answer")]);

console.log(lines.join("\n"));
console.log(`\n${lines.length - failed}/${lines.length} passed`);
process.exit(failed ? 1 : 0);
