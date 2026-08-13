/*
 * ESLint config for static/app.js.
 *
 * Exists because that file used to be a 3,265-line string inside
 * newspaper.py, where no tool could reach it. Every browser global it uses is
 * declared explicitly rather than pulled from the `globals` package, so this
 * config needs no dependency and CI can run it with a bare `npx eslint`.
 *
 * The rule set is deliberately narrow: correctness errors only, no style. A
 * style rule that fails the build teaches people to skip the build. These are
 * the rules that catch the class of bug that has actually shipped here — an
 * undefined identifier that aborts the entire script block on load.
 */
export default [
  {
    files: ["static/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        // Browser surface actually used by app.js. Anything not listed here
        // is a genuine typo, which is the whole point of no-undef.
        window: "readonly",
        document: "readonly",
        console: "readonly",
        fetch: "readonly",
        location: "readonly",
        history: "readonly",
        navigator: "readonly",
        localStorage: "readonly",
        sessionStorage: "readonly",
        setTimeout: "readonly",
        // Used by the stock screen's debounced search. Added when that landed —
        // the list is explicit precisely so a new global is a deliberate entry
        // rather than a silent pass.
        clearTimeout: "readonly",
        setInterval: "readonly",
        requestAnimationFrame: "readonly",
        IntersectionObserver: "readonly",
        URLSearchParams: "readonly",
        FormData: "readonly",
        Event: "readonly",
        confirm: "readonly",
        prompt: "readonly",
      },
    },
    rules: {
      // The one that matters. `el is not defined` took the whole page down on
      // 2026-08-08 — ticker, world map and scroll-spy — because one call sat
      // outside the scope that defines it.
      "no-undef": "error",

      // Everything else here is a silent-wrong-answer bug, not a style note.
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-func-assign": "error",
      "no-obj-calls": "error",
      "no-sparse-arrays": "error",
      "no-unreachable": "error",
      "valid-typeof": "error",
      "no-cond-assign": ["error", "always"],
      "no-compare-neg-zero": "error",
      "no-dupe-else-if": "error",
      "no-duplicate-case": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-unsafe-negation": "error",
      "use-isnan": "error",
      // NaN clamping to a perfect band score is how two unpriceable symbols
      // reached the top-5 ranking. Comparing against NaN is never intended.
      "no-constant-condition": ["error", { checkLoops: false }],
    },
  },
];
