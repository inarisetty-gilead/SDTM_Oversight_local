// The SAS function labels, in a file of their own: derivation.tsx and PipelineEditor.tsx
// both need them AND each other's components, and a shared constant inside either file
// makes the import graph circular — which the bundler tolerates only until the module
// order shifts, and then the app dies at load with "cannot access before initialization".
export const FN_LABELS: Record<string, string> = {
  substr: "SUBSTR — part of the text", scan: "SCAN — nth word",
  strip: "STRIP — trim both ends", trim: "TRIM — trim trailing blanks",
  left: "LEFT — trim leading blanks", compress: "COMPRESS — remove characters",
  upcase: "UPCASE — uppercase", lowcase: "LOWCASE — lowercase",
  propcase: "PROPCASE — Title Case", reverse: "REVERSE — reverse the text",
  length: "LENGTH — number of characters", index: "INDEX — position of text",
  tranwrd: "TRANWRD — find and replace", catx: "CATX — join with a separator",
  cats: "CATS — join, trimming each", cat: "CAT — join as-is",
  coalesce: "COALESCE — first non-missing", compbl: "COMPBL — squeeze blanks",
  zeropad: "Zero-pad to a width", put: "PUT — number to text", input: "INPUT — text to number",
}
