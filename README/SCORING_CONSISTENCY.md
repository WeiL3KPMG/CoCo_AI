# Scoring Consistency Settings

Recommended pipeline settings for consistency:

- `temperature = 0`
- `top_p = 1`
- use JSON mode or JSON schema mode if available
- cache completed outputs by input hash
- retry HTTP 429 errors with exponential backoff

Input hash should include:

- `target_text`
- `candidate_json`
- `prompt_version`
- `rubric_version`
- `model_name`
- `temperature`
- `top_p`

Consistency evaluation standard:

- Exact score match: ideal but not required.
- Score movement within +-5: acceptable.
- Score movement above +-5: high-variance case requiring review.
- Tier and rank changes: material inconsistency.
