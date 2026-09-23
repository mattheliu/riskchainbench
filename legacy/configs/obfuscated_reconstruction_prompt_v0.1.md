# CovertEntryWalker Task 1 Reconstruction Prompt v0.1

You are evaluating reconstruction, not web browsing or fraud judgment.

Input contains a platform name and a private-message session. Some messages may mix
platform-specific emoji tokens or rendered emoji, homophones, visually confusable
Unicode characters, spaces, and line breaks.

Recover only what is supported by the input:

1. Reconstruct each obfuscated target message into normalized plain text.
2. Recover the operation intent.
3. Return up to five candidate entries in descending confidence order.
4. Identify the platform. Do not use domain reputation, DNS, search, or browsing.
5. Abstain when the entry cannot be reconstructed reliably.

Return one JSON object matching
`schemas/obfuscated_reconstruction_prediction_v0.1.schema.json`.

Do not include hidden reasoning, a fraud label, webpage evidence, or any URL that was
not reconstructed from the supplied session.
