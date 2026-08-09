# Player Rating Contract V1

This internal-only layer turns current analysis evidence into an honest dashboard-ready summary. It does not modify public JSON, or invoke detectors, tracking, video, GPU, or network operations.

Supported categories are Technical Skill (the existing technical score), Physical Activity (the existing image-space activity score), and Ball Involvement. Scores are clamped 0–100 and versioned. Existing Technical and Physical formulas are preserved. Ball Involvement is `min(100, 100 * (interaction duration + controlled-movement duration) / 5 seconds)`; five seconds is a centralized provisional display scale.

Overall uses only available categories: Technical 0.45, Physical Activity 0.30, Ball Involvement 0.25, normalized for missing categories. It is unavailable below two categories. Confidence is separate: category confidence is preserved; overall confidence is reduced for short evidence and fewer categories.

Ball Involvement needs an interaction and existing 0.60 evidence coverage. Existing scorer availability gates Technical and Physical. Levels: very_low <20, low <35, developing <50, moderate <65, good <80, very_good <90, excellent >=90.

Physical Activity is never fitness; it is visible image-space activity. Ball proximity does not prove possession and candidate events are not confirmed actions. Soccer Intelligence, Tactical Vision, Mental Stability, Professionalism, Growth Potential, Market Readiness, and Scalability return `unsupported_by_current_pipeline`: short isolated video cannot support tactical, psychological, or scouting claims.

Tests cover availability, gates, no-zero substitution, unsupported categories, overall behavior, separated score/confidence, clamping, level boundaries, limitations, and deterministic versioning.

Next phase: **Public JSON V2 design**.
