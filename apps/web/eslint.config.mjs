import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypeScript from "eslint-config-next/typescript";

export default defineConfig([
  ...nextVitals,
  ...nextTypeScript,
  {
    rules: {
      "react-hooks/incompatible-library": "off",
      "react-hooks/set-state-in-effect": "off",
      "no-restricted-syntax": [
        "error",
        {
          selector: "CallExpression[callee.name='parseFloat']",
          message: "Use Decimal for financial values; parseFloat destroys precision."
        },
        {
          selector: "CallExpression[callee.name='Number']",
          message: "Use Decimal for financial values; Number destroys precision."
        }
      ]
    }
  },
  globalIgnores([".next/**", "coverage/**", "next-env.d.ts"])
]);
