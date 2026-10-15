# FIT_PLAN: getting real evidence that this fits the job

The challenge asks whether you talked to someone who does the work. This plan turns one honest conversation into evidence that cannot be faked: a review recorded against the exact briefs the person saw, and a `FIT.md` generated from it. Nothing here can be invented by the team or by an AI, and the tooling refuses to try.

## 1. Pick one person (start with one)
Best: someone who runs tenders or has sat on an evaluation committee (a county or national procurement unit, a state corporation, a university procurement office). Also good: an SME support desk or chamber of commerce officer, a small supplier who bids, an auditor or watchdog analyst. Use your own network or the competition community channel. One real person beats ten ignored emails.

## 2. Prepare the briefs (do not skip)
- Load the real export, then pick 2 or 3 **closed** real tenders (never a live one). Run the agent on each.
- `python -m bidbox.review packet --filing ID --filing ID -o packet.md` produces what you send. It shows each brief's fingerprint and says plainly whether the brief came from a model or the stub, and whether the data is synthetic.
- Until real data is loaded, a packet built from synthetic data is only good for feedback on layout and usefulness, and the report will say so.

## 3. The session (25 to 30 minutes)
1. **Consent, read aloud:** may we use your feedback; may we name you; your organisation; quote you. Any "no" is respected and the report will anonymise.
2. **Their real process (5 min).** How do you find out about a tender? What do you check first? What takes longest? Which data do you wish you had? Who signs off? What mistakes are expensive? What would make you distrust a tool like this?
3. **Timed task.** For one brief, they do their usual way first, then with the brief; for another, the other order. Questions: the deadline, the eligibility, the buyer's recent awards in this category, whether the estimate is in line with past awards, how long the bidding window was. Write down minutes for both. Do not coach.
4. **Review each brief:** what was wrong, what was missing, what to remove, ratings 1 to 5, would you use it again.

## 4. Record it
`python -m bidbox.review template > review.json`, fill it in (read it back to them if you can), then `python -m bidbox.review record review.json`. The tool rejects a review without consent, without a real role, with a brief the person did not see, or with ratings out of range.

## 5. Let it change the product
When feedback makes you change something: `python -m bidbox.review change "what you changed" --because REVIEW_ID --commit HASH`. It can only be attributed to feedback that exists.

## 6. Generate the report
`python -m bidbox.review fit -o FIT.md`. It computes everything, includes every criticism verbatim, names only with consent, and labels small samples as anecdotes.

## Do not
Fill a review yourself, use a placeholder person, use a live tender or anyone's personal data, quote numbers the report does not contain, or claim a whole evaluation went from weeks to minutes. The report says what was timed, for how many tasks, and nothing more.
