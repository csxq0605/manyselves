import eslint from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  { ignores: ["dist/**", "release/**"] },
  {
    files: ["src/**/*.ts"],
    languageOptions: { globals: globals.node },
    rules: { "@typescript-eslint/consistent-type-imports": "error" },
  },
  {
    files: ["scripts/**/*.mjs"],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["src/preload.cjs"],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: { "@typescript-eslint/no-require-imports": "off" },
  },
);
