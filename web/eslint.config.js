import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

// Flat-config ESLint for the timeline UI. Mirrors the Python side's strict
// posture: TypeScript recommended + React Hooks + react-refresh scoping.
export default tseslint.config(
  { ignores: ["dist/**"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      // We use `unknown` freely for arbitrary OTel payload; the runtime
      // inspector doesn't (and shouldn't) know the shape of every value.
      "@typescript-eslint/no-explicit-any": "off",
      // We assert at trust boundaries (API ingest); narrowing afterwards.
      "@typescript-eslint/no-unsafe-assignment": "off",
      "@typescript-eslint/no-unsafe-member-access": "off",
    },
  },
);
