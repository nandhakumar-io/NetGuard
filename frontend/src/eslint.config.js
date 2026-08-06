import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // eslint-plugin-react-hooks@7's "recommended" preset ships a wide
      // set of React Compiler-oriented rules (set-state-in-effect,
      // purity, immutability, ...) that assume patterns this codebase
      // doesn't follow (e.g. `useEffect(load, [])` data-fetching, used
      // throughout). Opt into just the two classic, uncontroversial
      // rules instead of pulling in a large disruptive rewrite.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // Prototype/dashboard codebase leans on `any` at API-response
      // boundaries (axios error handling, third-party SDK payloads);
      // enforcing this as an error would mean a large disruptive sweep
      // for no real safety gain there. Keep it a warning instead.
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  }
);