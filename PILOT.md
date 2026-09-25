# Pilot plan: one coach, a few students, two weeks

Goal: find out whether students come back, what they ask that the coach can't answer well, and what breaks.
Not a goal: polish, scale, or selling.

## Before day 1 (coach setup, ~2 hours)

- [ ] Install on the coach's machine (`docker compose up --build`), native Ollama on a Mac, confirm the header shows `LLM ✓` and `voice server`.
- [ ] Coach opens `/coach`, edits Ms. Ada's name to their own in `COACH_NAME`, and adds **three lessons of their own** (their words, their examples). This is the point of the pilot: the avatar must sound like them.
- [ ] Coach records 3 minutes of clean audio for voice cloning later (not needed for the pilot).
- [ ] Each student: 5-minute onboarding call. Type your name, allow the microphone, try "show me a knight fork", review one chess.com game.
- [ ] Consent: parents told it is an AI assistant, sessions are logged for the coach, no recordings leave the machine. Written OK before the first session.

## During the two weeks

Students are asked to do three things, in any order, at least three times a week:
1. Review one of their own games with the coach avatar.
2. Do one lesson.
3. Ask at least one question they'd normally save for the human coach.

The human coach does nothing different, except a weekly 20-minute look at the data below.

## What to log (all automatic)

| What | Where | Why |
|---|---|---|
| Every question and answer | `/api/questions.csv` | The list of questions the avatar answered badly *is the roadmap* |
| Games reviewed, mistakes by type | `data/tutor.db` (profile card in the app) | Are recurring mistakes actually going down? |
| Lessons completed | same | Do students finish lessons or bail? |
| Problem reports | `data/reports/*.json` | Every "it didn't understand me" with the transcript |
| Sessions per student | profile card | The only metric that matters at this stage: **do they come back?** |

## Weekly coach review (20 minutes)

1. Open the questions CSV. Mark each answer: good / vague / wrong / avatar couldn't do it. Wrong and couldn't-do-it become tickets.
2. Look at the reports folder. Group by cause (mic, voice, engine, content, confusing UI).
3. Pick one thing to add to the content editor for next week, based on what students asked.

## Session observation (do this for at least two live sessions)

Sit behind the student, say nothing, and note:
- Time from opening the page to the first useful exchange (target under 60 s).
- Every moment the student waits without knowing what's happening.
- Every time they repeat or rephrase a question (the avatar didn't get it).
- What they do when the avatar is wrong: correct it, ignore it, or trust it.
- Whether they look at the board or the avatar while she talks.

## Success criteria at the end of two weeks

- 3 of 4 students used it in week 2 without being reminded.
- The coach added lessons on their own without asking how.
- At least one "she caught something I always do" moment from a student or parent.
- Fewer than 1 in 5 questions marked wrong.

If those hold, the next steps are cloned voice, real face, accounts, and a second coach. If they don't, the question log tells us why.

## Questions to ask the coach on day 14

- Would you pay for this? What would you pay per month? What would make you stop?
- Which students used it most, and why those?
- What did you learn about your students from the data that you didn't know?
- What did it say that embarrassed you?
