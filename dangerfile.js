// The repo's first rule, enforced on every PR: never self-invent. Runs from the base branch.
// Framework: danger/danger-js@14.1.0. Rules below only read the PR's own body and file list.
const { danger, fail, schedule } = require("danger");

// `danger local` runs this file without PR metadata; `just pr` passes the body through
// LOCAL_PR_BODY_FILE and the check reads the manifest from the commit it would push.
const local = !danger.github;
const pr = local ? { user: { login: "local" } } : danger.github.pr;
const body = local ? localBody() : (pr.body ?? "");
function localBody() {
  const path = process.env.LOCAL_PR_BODY_FILE;
  if (!path) {
    fail("PR body: LOCAL_PR_BODY_FILE is not set; run `bash scripts/pr-check.sh <body.md>` or `just pr`.");
    return "";
  }
  try {
    return require("fs").readFileSync(path, "utf8");
  } catch (error) {
    fail(`PR body: LOCAL_PR_BODY_FILE ${path} cannot be read: ${error.message}`);
    return "";
  }
}
function localManifest() {
  try {
    return require("child_process").execSync("git show HEAD:docs/manifest.md", { encoding: "utf8" });
  } catch (error) {
    fail(`Manifest row: git show HEAD:docs/manifest.md failed: ${error.message}`);
    return "";
  }
}
const section = (name) => {
  const match = body.match(new RegExp(`^## ${name}\\s*$([\\s\\S]*?)(?=^## |(?![\\s\\S]))`, "m"));
  // Preserve the first list marker's indentation so its peers cannot become children.
  return (match?.[1] ?? "").replace(/<!--[\s\S]*?-->/g, "").trimEnd().replace(/^(?:[ \t]*\r?\n)+/, "");
};

const sources = section("Upstream references");
// `owner/repo@tag`, or the manifest's own `[owner/repo](url)@tag`, then the full commit SHA.
const repoAtCommit = /[\w.-]+\/[\w.-]+(?:\]\([^)\s]*\))?@\S*[\s\S]*?\b[0-9a-f]{40}\b/;
const docsWithDate = /https?:\/\/\S+[\s\S]*?read 20\d\d-\d\d-\d\d/;
const paper = /(doi\.org\/10\.|\bdoi:10\.|arxiv\.org\/abs\/|\barXiv:\d)/i;

// Dependabot's generated body cites the upstream's own release notes, and it changes pins only.
if (pr.user.login !== "dependabot[bot]") {
  for (const name of ["What changed", "Manifest row", "Upstream references", "How verified", "How to revert"]) {
    if (!section(name)) fail(`PR body: "${name}" is empty.`);
  }

  if (!repoAtCommit.test(sources) && !docsWithDate.test(sources) && !paper.test(sources)) {
    fail(
      "Never self-invent: Upstream references must name a source repo@tag with its full commit SHA, " +
        "a docs page with `read YYYY-MM-DD`, or a paper (DOI or arXiv).",
    );
  }

  const named = [...section("Manifest row").matchAll(/"([^"]+)"/g)].map((match) => match[1]);
  schedule(async () => {
    const manifest = local
      ? localManifest()
      : await danger.github.utils.fileContents("docs/manifest.md");
    const rows = manifest.split("\n").filter((row) => !row.startsWith("| Practice or pick |"));
    if (!named.some((name) => rows.some((row) => row.startsWith(`| ${name} |`)))) {
      fail('Manifest row: name a row of docs/manifest.md in "double quotes". This PR may add it.');
    }

    // A third-party plugin row carries the disclosure its admission depends on.
    const register = manifest.split(/^## /m).find((part) => part.startsWith("Third-party plugins\n"));
    if (!register) return;
    const pluginRows = register
      .split("\n")
      .filter((row) => row.startsWith("| ") && !row.startsWith("| Practice or pick |") && !row.startsWith("|---"));
    for (const name of named) {
      const row = pluginRows.find((line) => line.startsWith(`| ${name} |`));
      if (row && !/capabilit|red line|red-line/i.test(row)) {
        fail(`Third-party plugin row "${name}" must record its capability and red-line disclosure.`);
      }
    }
  });
}

const changed = [...danger.git.created_files, ...danger.git.modified_files, ...(danger.git.deleted_files ?? [])];
// Sextant's source rule (see ORIGINS.md), read for this package. Sextant applied it only to PRs
// created after a cutover; this repository has no earlier PRs, so it applies to every PR.
if (pr.user.login !== "dependabot[bot]") {
  schedule(async () => {
    // BUILD ON danger/danger-js@14.1.0 72540533c4f99bad772ba303be0eb4af6eddbdc4:
    // GitDSL.ts:24-27,157 exposes fromPath;
    // gitJSONToGitDSL.ts:194-213 preserves rename origins lost by diffToGitJSONDSL.ts:22.
    let touchesSource = changed.some((path) => path.startsWith("src/sextant_movers/"));
    if (!touchesSource) {
      const diffs = await Promise.all(danger.git.modified_files.map((path) => danger.git.structuredDiffForFile(path)));
      touchesSource = diffs.some((diff) => diff?.fromPath?.startsWith("src/sextant_movers/"));
    }
    if (!touchesSource) return;
    // KEEP OURS: no Markdown parser in Danger's runtime or our gate dependencies.
    // https://spec.commonmark.org/0.31.2/#list-items, read 2026-11-11: all five marker forms;
    // a nested block's indentation includes the parent's marker width and padding.
    const lines = sources
      .replace(/^[ \t]*\|[^\n]*\n[ \t]*\|[ \t:|\-]+\|[ \t]*$/gm, "")
      .split("\n");
    const entries = [];
    let entryLines = [], contentIndent = null, blank = false;
    const columns = (text) => [...text].reduce((n, ch) => n + (ch === "\t" ? 4 - n % 4 : 1), 0);
    for (const line of lines) {
      if (!line.trim()) {
        blank = true;
        continue;
      }
      const indent = columns(line.match(/^[ \t]*/)[0]);
      const marker = line.match(/^([ \t]*)([-+*]|\d{1,9}[.)])([ \t]+)(.*)$/);
      const nested = contentIndent !== null && indent >= contentIndent;
      const field = marker && /^(Verdict|Where searched):/i.test(marker[4]);
      if (!nested && !field && (marker || /^\s*\|/.test(line) || blank)) {
        if (entryLines.length) entries.push(entryLines.join("\n").trim());
        entryLines = [];
        contentIndent = null;
      }
      if (marker && !nested && !field) {
        const markerEnd = columns(marker[1] + marker[2]);
        const padding = columns(marker[1] + marker[2] + marker[3]) - markerEnd;
        contentIndent = markerEnd + (padding <= 4 ? padding : 1);
      }
      entryLines.push(line);
      blank = false;
    }
    if (entryLines.length) entries.push(entryLines.join("\n").trim());
    for (const [index, entry] of (entries.length ? entries : [""]).entries()) {
      const missing = [];
      if (!repoAtCommit.test(entry) && !docsWithDate.test(entry)) {
        missing.push("a pinned source (repo@tag with full commit SHA, or docs page with read YYYY-MM-DD)");
      }
      // Template code spans may enclose just the path or the entire location. Greedy
      // filenames leave the final :digits as the line; a URL's port is not a location.
      const locations = [...entry.replace(/`/g, "").matchAll(/(?:^|[\s(|])([\w./:-]+):(\d+)\b/g)];
      if (!locations.some(([, path, line]) => !path.includes("://") && Number(line) > 0)) {
        missing.push("path:line at that pin");
      }
      // The verdict vocabulary of the PR template's Upstream references comment.
      if (!/\b(use as is|build on|keep ours)\b/i.test(entry)) {
        missing.push("a verdict (USE AS IS / BUILD ON / KEEP OURS)");
      }
      if (/\bkeep ours\b/i.test(entry) && !/\bWhere searched:[ \t]*\S[^\n]*/i.test(entry)) {
        missing.push("Where searched: paths and search terms for KEEP OURS");
      }
      if (missing.length) {
        fail(
          "Never self-invent: src PRs need Upstream references for each mechanism; " +
            `entry ${index + 1} is missing ${missing.join("; ")}.`,
        );
      }
    }
  });
}
// One upstream owns each surface (SOURCE-HIERARCHY.md). A governed path must name it,
// so a fork or a second SDK fails here instead of landing as a quiet second owner.
// TradingView's own sources alone govern the display; the MCP SDK is cited as its transport, in
// addition, and never stands in for TradingView.
const AUTHORITY = {
  ranking: {
    paths: ["src/sextant_movers/ranking/"],
    owners: [
      "nautechsystems/nautilus_trader",
      "alpacahq/alpaca-py",
      "docs.alpaca.markets",
      "gerrymanoim/exchange_calendars",
    ],
  },
  tradingview: {
    paths: ["src/sextant_movers/tradingview/"],
    owners: ["tradingview.com", "www.tradingview.com", "tradingview.github.io", "tradingview/lightweight-charts"],
  },
};
if (pr.user.login !== "dependabot[bot]") {
  const cited = sources.toLowerCase();
  const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const names = (owner) => {
    // A repo is cited as owner/repo, in a link's text, or in a URL path; a domain stands alone.
    return new RegExp(`(^|[^\\w.-])${escape(owner)}(?![\\w.-])`);
  };
  for (const [surface, rule] of Object.entries(AUTHORITY)) {
    const path = changed.find((file) => rule.paths.some((prefix) => file.startsWith(prefix)));
    if (!path) continue;
    if (!rule.owners.some((owner) => names(owner).test(cited))) {
      fail(
        `Never self-invent: ${path} is governed by the ${surface} surface, but Upstream references ` +
          `name no authority for it. Cite one of ${rule.owners.join(", ")} (SOURCE-HIERARCHY.md).`,
      );
    }
  }
}
