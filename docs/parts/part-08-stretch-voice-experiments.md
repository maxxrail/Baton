# Part 08 (stretch) — Voice channel + AOP experiments

Only start this after Part 07 is live.

## A. Voice
- Twilio Voice inbound number → media stream WebSocket → streaming STT → agent runtime → streaming TTS back. Keep providers behind `STT`/`TTS` interfaces (pick ones with good latency; document the choice and measured latency).
- Voice-specific behavior: short replies, barge-in (stop speaking when the caller talks), DTMF/spoken verification of order number + zip, warm transfer to a human number on escalation.
- Same AOPs, guardrails, traces, and Watchtower scoring as text channels; add voice simulations (persona text → TTS → the voice pipeline) for at least 5 scenarios.
- Acceptance: call the number and complete "where is my order" end to end; p50 time-to-first-audio measured and recorded in EVAL_REPORT.md.

## B. Experiments (A/B testing AOP versions)
- Run two versions of an AOP side by side with a traffic split (sticky per customer), configured in `experiments.yaml`.
- Report per arm: automated resolution rate, escalation rate, Watchtower scores, turns, cost, with confidence intervals (bootstrap). Simple "promote winner" action in the console.
- Acceptance: run an experiment over generated traffic (Part 06 script) where one arm is deliberately better; the report detects it.
