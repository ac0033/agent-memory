"""自建开发集 dev-qa：按能力维度分桶的问答探针（memory-v1-design.md §6 的迭代用尺子）。

为什么要有它：外部评测集原则上每个只测一次（反复测一是过拟合，二是太贵）。机制迭代期间的
验证靠这一套——题目按能力体系（框架 §5.1）的定义出，不看任何外部集的题面。

形状与 LongMemEval 相同（可直接喂给 runners/aml_selftest.py，走同一套答题模板），但有两点不同：
- **所有题共享同一份历史**：嵌入与蒸馏只付一次，之后每轮迭代只花答题调用；
- 能确定性判分的题带 `rubric`（all_of / any_of / none_of，小写正则），runner 见到 rubric 就不调评委——
  零成本、零评委噪声。只有开放式的偏好题与错误前提题才用评委（gold 写成评分要点）。

| 桶 | 能力 | 考什么 |
|---|---|---|
| det-u | K4 | 用户顺口提过一次的细节 |
| det-a | K4 / K3 | 助手说过的细节（蒸馏通常不沉淀助手的话，只能靠原文） |
| upd | K5 | 值变过：问现在的、问之前的 |
| tmp | K5 | 相对时间（"昨天""上周六""三周前"）→ 绝对日期与间隔 |
| agg | K6 | 散在多场会话的实例：计数、求和、列举；陷阱按"记录数≠实例数"的成因埋 |
| pref | K11 | 开放式求助，答案应体现用户说过的偏好与约束 |
| abs | K12 | 前提不成立的近似题，应当说没有依据 |

题目与会话全部手写，不用 LLM 生成：金标逐字可控。

    uv run python docs/research/benchmark-suite/build/build_dev_qa.py
    → data/external/dev_qa_v1.json
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
OUT = REPO / "data" / "external" / "dev_qa_v1.json"
QUESTION_DATE = "2024/03/30 (Sat) 10:00"

U, A = "user", "assistant"

# (会话 id, 日期, 轮次)。证据是顺口带出来的——用户在问别的事，事实夹在里面，和真实使用一样。
SESSIONS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("e01", "2024/01/08 (Mon) 09:12", [
        (U, "Can you help me draft a polite email to reschedule a cleaning? My dentist, Dr. Okafor over on Hillcrest Ave, only had a Thursday slot and I have a client call then."),
        (A, "Of course. Here's a draft: 'Hello, I have a cleaning booked for Thursday but a work conflict has come up. Could we move it to any morning next week? Thank you for your flexibility.' Offering a range of alternatives usually gets a faster reply than asking for one specific time."),
        (U, "Nice, short and clear. I'll send it."),
        (A, "Good luck. If they only have afternoons, ask to be put on the cancellation list as well."),
    ]),
    ("e02", "2024/01/10 (Wed) 19:40", [
        (U, "I'm trying to get better at SQL for work. Any book suggestions?"),
        (A, "Three that work well in order: first 'Learning SQL' by Alan Beaulieu for fundamentals, then 'SQL Antipatterns' by Bill Karwin once you write queries daily, and finally 'High Performance MySQL' if you end up tuning databases. Start with the first and do every exercise."),
        (U, "Great, I'll order the first one. I still work at Brightline Logistics and most of what I do there is reporting, so fundamentals are what I need."),
        (A, "Then Beaulieu is the right start. For reporting work, focus on joins, grouping and window functions."),
    ]),
    ("e03", "2024/01/12 (Fri) 08:05", [
        (U, "What's a reasonable way to split chores with a partner when one person travels a lot?"),
        (A, "Split by ownership rather than by task count: each person fully owns a few areas, and travel weeks trigger a pre-agreed swap. A shared checklist helps, and a ten-minute Sunday review keeps resentment from building up."),
        (U, "Makes sense. By the way our rent is $1,450 a month right now, so we're also trying to work out a fair split of that."),
        (A, "A common approach is to split rent in proportion to take-home income rather than 50/50, and revisit whenever either income changes."),
    ]),
    ("e04", "2024/01/15 (Mon) 21:30", [
        (U, "Two months ago I started learning Portuguese and I feel stuck at the beginner plateau. Tips?"),
        (A, "Plateaus at two months are normal. Switch from apps to input you enjoy: a podcast for learners, graded readers, and one weekly conversation session. Track hours of listening rather than streaks."),
        (U, "Helpful. I grew up speaking Hindi and English at home, so at least I'm used to switching languages."),
        (A, "That is a real advantage. Bilinguals usually pick up a third language's rhythm faster. Lean on listening."),
    ]),
    ("e05", "2024/01/17 (Wed) 12:20", [
        (U, "I bought a monstera last weekend and one leaf is yellowing. What should I check?"),
        (A, "Check watering first: yellowing on a new monstera is usually overwatering. Let the top two inches dry out, make sure the pot drains, and give it bright indirect light. Plan to repot it in late April, once it is actively growing; repotting now in winter would stress it."),
        (U, "Okay, I'll hold off on repotting then."),
        (A, "Good. One yellow leaf after a move is normal; watch for the pattern continuing."),
    ]),
    ("e06", "2024/01/20 (Sat) 10:15", [
        (U, "Went to see Khruangbin last night, amazing show. Can you recommend similar bands?"),
        (A, "If you liked Khruangbin, try Glass Beams, Altin Gun, Mildlife and Yin Yin. All lean on groove, psychedelic guitar and global influences."),
        (U, "Adding them to my playlist. My sister Priya saw them in Leeds last year, that's where she lives, and she's been telling me to go ever since."),
        (A, "Then you can finally compare notes with her. Glass Beams is probably the closest match."),
    ]),
    ("e07", "2024/01/22 (Mon) 18:45", [
        (U, "I'm vegetarian and honestly I can't stand mushrooms. Every veggie recipe seems to be full of them. Any pantry staples you'd recommend?"),
        (A, "Keep chickpeas, lentils, tofu, halloumi, eggs if you eat them, tahini, miso, canned tomatoes and good stock. Those cover protein and depth of flavour without relying on mushrooms."),
        (U, "Perfect, that's a good list."),
        (A, "Smoked paprika and soy sauce are also good stand-ins for the savoury note mushrooms usually provide."),
    ]),
    ("e08", "2024/01/25 (Thu) 07:50", [
        (U, "I run about 15 miles a week, always in the mornings before work. How should I warm up when it's cold?"),
        (A, "Do five minutes indoors first: leg swings, lunges, high knees. Start the run at an easy pace for the first mile, and cover your hands and ears."),
        (U, "Will do. I have to be careful, my left knee has been bad since an old injury."),
        (A, "Then add glute bridges and side steps to the warm-up; hip strength takes load off the knee."),
    ]),
    ("e09", "2024/01/27 (Sat) 15:10", [
        (U, "I picked up a snake plant today to go next to the monstera. Does it need the same care?"),
        (A, "No, the snake plant wants far less water. Every three to four weeks in winter is plenty, and it tolerates low light."),
        (U, "Even easier than I thought."),
        (A, "It is one of the hardest plants to kill. Just do not overwater it."),
    ]),
    ("e10", "2024/01/30 (Tue) 20:05", [
        (U, "My sourdough keeps coming out dense. I use 500 g of bread flour."),
        (A, "Dense crumb usually means under-fermentation or too little water. For 500 g of bread flour I recommend 72% hydration, so 360 g water, 100 g active starter and 10 g salt. Bulk ferment until the dough has grown about 75 percent, not by the clock."),
        (U, "I was at about 65%, so that explains it."),
        (A, "Going to 72% will open the crumb. Wet your hands when shaping; the dough will be stickier."),
    ]),
    ("e11", "2024/02/02 (Fri) 13:30", [
        (U, "Finished Piranesi last night. What should I read next? I love literary fiction and science fiction, but I really dislike thrillers."),
        (A, "Given that, try 'The Overstory' by Richard Powers, 'Tomorrow, and Tomorrow, and Tomorrow' by Gabrielle Zevin, or 'A Memory Called Empire' by Arkady Martine. None of them is a thriller."),
        (U, "I'll start with the Zevin."),
        (A, "Good choice. It reads quickly despite the length."),
    ]),
    ("e12", "2024/02/05 (Mon) 09:25", [
        (U, "Good news: our landlord renewed the lease. The rent goes up to $1,525 starting this month though."),
        (A, "Congratulations on the renewal. A rise of $75 is about 5 percent, which is near the typical range. It is worth updating your budget categories now rather than at month end."),
        (U, "Yeah, I'll fix it in YNAB tonight. I track every dollar there, we're pretty budget-conscious."),
        (A, "Then adjust the rent category and pull the difference from a flexible category such as dining out."),
    ]),
    ("e13", "2024/02/08 (Thu) 18:00", [
        (U, "I need a backup command for my photos folder to the NAS."),
        (A, "Use: rsync -avz --delete ~/Photos/ nas:/volume1/photos/ . The -a flag preserves attributes, -v is verbose, -z compresses in transit, and --delete removes files on the NAS that you deleted locally. Do a dry run with -n first."),
        (U, "Dry run looks right. Running it for real now."),
        (A, "Good. Schedule it weekly with cron once you are happy with it."),
    ]),
    ("e14", "2024/02/12 (Mon) 08:40", [
        (U, "I adopted a cat yesterday! Her name is Miso. What do I need for the first week?"),
        (A, "Congratulations. Keep her in one quiet room for the first few days with a litter box, water, food, a hiding spot and a scratching post. Book a vet check within the first week and keep the food the shelter used."),
        (U, "She's already hiding under the bed."),
        (A, "That is normal. Sit in the room and let her come to you."),
    ]),
    ("e15", "2024/02/14 (Wed) 22:10", [
        (U, "Saw a local jazz trio at the Blue Door tonight. Small venue, great sound. How do I find more gigs like that?"),
        (A, "Follow the venue's mailing list, check the local listings site weekly, and ask the musicians where else they play; small scenes are tightly connected."),
        (U, "Good idea, I'll sign up."),
        (A, "Weeknight shows are usually cheaper and less crowded too."),
    ]),
    ("e16", "2024/02/17 (Sat) 11:00", [
        (U, "I put new tires on my bike today, $84 for the pair. Do I need to break them in?"),
        (A, "New tires have a slick release coating. Ride gently for the first 20 miles or so, especially in corners and on wet roads."),
        (U, "Noted."),
        (A, "Check the pressure again after the first ride; new tubes settle."),
    ]),
    ("e17", "2024/02/20 (Tue) 19:20", [
        (U, "We fostered a kitten for a week through the shelter. She went back yesterday to be adopted by another family. Miso seemed relieved, honestly."),
        (A, "Fostering for even a week helps a shelter a lot. It is common for a resident cat to relax once the visitor leaves. Give Miso a few days of normal routine."),
        (U, "She's back to her usual spot on the couch."),
        (A, "That is a good sign."),
    ]),
    ("e18", "2024/02/23 (Fri) 12:45", [
        (U, "I use Notion for all my notes; paper planners never stick for me. How can I stop my workspace from turning into a mess?"),
        (A, "Use one database for notes with a few properties, such as area, status and date, instead of nested pages. Add an inbox view, and review it weekly."),
        (U, "A single database sounds much saner."),
        (A, "Templates for recurring note types will save you time as well."),
    ]),
    ("e19", "2024/02/26 (Mon) 20:30", [
        (U, "I started a pottery class three weeks ago and my bowls keep collapsing on the wheel."),
        (A, "Collapsing usually means the walls are too wet or too thin at the base. Use less water, compress the base, and pull the walls in three passes rather than one."),
        (U, "I definitely use too much water."),
        (A, "Keep a sponge on a stick to take water out of the bottom as you go."),
    ]),
    ("e20", "2024/02/28 (Wed) 07:35", [
        (U, "I've built up to 22 miles a week now. Is that enough base for a half marathon?"),
        (A, "Yes. At 22 miles a week with one long run of 9 to 10 miles you are ready to finish comfortably. Taper the final week."),
        (U, "Race is this weekend, so I guess I'm tapering already."),
        (A, "Keep the last few runs short and easy, and sleep well on Thursday and Friday."),
    ]),
    ("e21", "2024/03/01 (Fri) 17:15", [
        (U, "Big change: today was my first day at Novacore Health. I left Brightline at the end of February."),
        (A, "Congratulations on the new role. In the first weeks, focus on learning who owns what and on a few small, visible wins."),
        (U, "Thanks. It's a data analyst role again, but on healthcare data."),
        (A, "Expect stricter privacy rules than in logistics; ask early about data access procedures."),
    ]),
    ("e22", "2024/03/04 (Mon) 08:20", [
        (U, "I ran the half marathon last Saturday! Finished in 2:04. My legs are wrecked."),
        (A, "Well done, 2:04 is a solid first half. Walk daily this week, keep runs very easy, and wait until next week for anything fast."),
        (U, "Walking is about all I can manage."),
        (A, "That is exactly what recovery should look like."),
    ]),
    ("e23", "2024/03/06 (Wed) 21:00", [
        (U, "Bought a new saddle for the bike, $59. My old one was killing me on longer rides."),
        (A, "Give a new saddle three or four rides before judging it. Level it first, then adjust the tilt by a degree at a time."),
        (U, "Okay."),
        (A, "If numbness continues, the width is probably wrong rather than the padding."),
    ]),
    ("e24", "2024/03/08 (Fri) 13:05", [
        (U, "Finished Tomorrow, and Tomorrow, and Tomorrow. Loved it. I told my book club about Piranesi again too, they still haven't read it."),
        (A, "Glad it landed. If the club wants something short, Piranesi is an easy sell at around 250 pages."),
        (U, "Our book club moved from Tuesdays to Thursdays, by the way, so I actually have time to read before it now."),
        (A, "Thursday gives you the early week to finish. What is the next pick?"),
    ]),
    ("e25", "2024/03/10 (Sun) 16:40", [
        (U, "I switched from my old Pixel 6 to an iPhone 15 this weekend. How do I move my two-factor codes?"),
        (A, "Export accounts from the authenticator app on the old phone using its transfer feature, scan the code on the new phone, and verify each account before wiping the Pixel. Keep backup codes for anything critical."),
        (U, "Doing that before I trade the Pixel in."),
        (A, "Check banking apps separately; many need re-enrolment."),
    ]),
    ("e26", "2024/03/12 (Tue) 18:30", [
        (U, "Can you work out a loan payment? $9,000 over 24 months at 9% APR."),
        (A, "With a monthly rate of 0.75 percent over 24 payments, the payment comes to about $411.16 per month, and total interest is roughly $868."),
        (U, "That's for the used car I'm looking at, to replace my 2017 Subaru Outback eventually. Not yet though."),
        (A, "If you can put more down, the interest drops quickly at that rate."),
    ]),
    ("e27", "2024/03/14 (Thu) 22:30", [
        (U, "Mitski was incredible tonight. Third show of the year for me and easily the best."),
        (A, "Her live arrangements are very different from the records. Did she play much from the new album?"),
        (U, "About half. I already have tickets for The National in May too."),
        (A, "That should be a good one; they tend to play long sets."),
    ]),
    ("e28", "2024/03/16 (Sat) 10:50", [
        (U, "Added a pothos to the plant shelf. I'm also eyeing a fiddle-leaf fig but haven't bought one, they seem fussy."),
        (A, "Pothos is forgiving and will trail nicely from a shelf. Fiddle-leaf figs are fussy about drafts and being moved, so only get one if you have a bright, stable spot."),
        (U, "I'll wait on the fig then."),
        (A, "Sensible. See how the current plants do through spring first."),
    ]),
    ("e29", "2024/03/18 (Mon) 12:10", [
        (U, "My renewed passport arrived in the mail today, finally. What should I double check on it?"),
        (A, "Check the spelling of your name, date of birth, the expiry date and that the photo page is undamaged. Sign it if your country requires a signature."),
        (U, "All good."),
        (A, "Take a photo of the data page and store it somewhere safe."),
    ]),
    ("e30", "2024/03/20 (Wed) 19:45", [
        (U, "My flight to Lisbon is in exactly three weeks. I always book morning departures and an aisle seat because of my knee. Anything I should prepare?"),
        (A, "Check that your passport is valid six months past travel, download offline maps, tell your bank, and get a Viva Viagem card on arrival for transit."),
        (U, "Passport's sorted."),
        (A, "Then it is mostly packing. Lisbon is hilly, so bring shoes with grip."),
    ]),
    ("e31", "2024/03/22 (Fri) 17:55", [
        (U, "My daughter's piano lessons changed. Mrs. Alvarez retired, so she's with Mr. Chen now. How can I help her adjust to a new teacher?"),
        (A, "Let her play a piece she is proud of at the first lesson, share what worked with the previous teacher, and keep the practice routine unchanged for the first month."),
        (U, "Good thinking."),
        (A, "New teachers often change technique details; reassure her that is normal."),
    ]),
    ("e32", "2024/03/24 (Sun) 09:30", [
        (U, "Recommend a hike within an hour of the city? Something moderate."),
        (A, "Try the Cedar Ridge Loop: 7.4 miles with about 1,200 feet of climbing, mostly shaded, with a creek crossing at the halfway point. Start before 9 am for parking."),
        (U, "Sounds ideal. My dog Biscuit will love the creek."),
        (A, "Dogs are allowed on leash there. Bring water for both of you."),
    ]),
    ("e33", "2024/03/26 (Tue) 20:15", [
        (U, "Got front and rear lights for the bike, $37. Now that I commute to the new office I ride home at dusk."),
        (A, "Run the rear light in steady mode rather than flashing at dusk, and angle the front light slightly down so it does not dazzle oncoming riders."),
        (U, "Done."),
        (A, "Reflective ankle bands add a lot of visibility for very little money."),
    ]),
    ("e34", "2024/03/28 (Thu) 13:20", [
        (U, "Finished The Overstory. Heavy but worth it. Also, random: my gym locker code is 4182, I keep forgetting it, and I'm allergic to cashews so remind me never to buy that trail mix again."),
        (A, "Noted on both. For the locker code, a password manager's secure note is safer than memory. For trail mix, check labels for 'may contain tree nuts' as well as the ingredient list."),
        (U, "Good call."),
        (A, "The Overstory is a lot to absorb; something lighter next might be welcome."),
    ]),
    ("e35", "2024/02/10 (Sat) 14:00", [
        (U, "I ordered a standing desk from Fernwood Office, $389 with free shipping. How high should I set it?"),
        (A, "Set it so your elbows are at about 90 degrees with relaxed shoulders, and the top of the screen at or just below eye level. Alternate sitting and standing every 30 to 45 minutes."),
        (U, "I'll set a timer."),
        (A, "An anti-fatigue mat makes standing far more comfortable."),
    ]),
    ("e36", "2024/01/28 (Sun) 11:30", [
        (U, "My wifi drops in the bedroom. The router is an Asus RT-AX58U in the living room."),
        (A, "The RT-AX58U supports mesh through AiMesh. Either move it to a more central spot, or add a second AiMesh node near the bedroom. Check that the 5 GHz channel is not set to a crowded one."),
        (U, "I'll try moving it first."),
        (A, "Height helps as well; get it off the floor."),
    ]),
    ("e37", "2024/01/13 (Sat) 09:45", [
        (U, "Planning how to use my vacation days. Last year I went to Portugal in the spring, which is why I started learning the language."),
        (A, "A spring trip is a good pattern: fewer crowds and lower prices. Spread remaining days into long weekends so you get regular breaks."),
        (U, "Good idea."),
        (A, "Booking flights eight to ten weeks ahead is usually the price sweet spot for Europe."),
    ]),
    ("e38", "2024/02/16 (Fri) 20:40", [
        (U, "Going through last year's photos. The Japan trip in October was the highlight, the food alone was worth it."),
        (A, "October is a great time for Japan. If you want to make a photo book, pick thirty images and arrange them by day rather than by place."),
        (U, "Thirty is going to be hard."),
        (A, "Start with sixty and cut in half; the second cut is easier than the first."),
    ]),
    ("e39", "2024/03/02 (Sat) 12:30", [
        (U, "Found the receipts from our Mexico trip last December while doing taxes. Do I need to keep them?"),
        (A, "Personal travel receipts are not needed for taxes unless part of the trip was deductible business travel. Keep anything tied to a warranty or insurance claim."),
        (U, "Into the recycling they go."),
        (A, "Scan anything you are unsure about first."),
    ]),
]

# 底噪：同类词出现在库里，但不是用户自己的实例——助手的举例、别人的事、泛泛的咨询。
FILLERS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("f01", "2024/01/09 (Tue) 10:00", [
        (U, "What are good low-light houseplants for an office?"),
        (A, "ZZ plants, snake plants, pothos, peace lilies and cast iron plants all tolerate low light. Many offices keep five or six of these."),
        (U, "Thanks, it's for a colleague."),
        (A, "ZZ plants are the most forgiving of neglect."),
    ]),
    ("f02", "2024/01/18 (Thu) 16:20", [
        (U, "My sister went to three concerts in one week at a festival. Is that bad for your hearing?"),
        (A, "Repeated loud exposure adds up. High-fidelity earplugs reduce volume without muffling the music, and they are cheap."),
        (U, "I'll tell her."),
        (A, "Standing away from the speakers helps more than people expect."),
    ]),
    ("f03", "2024/01/24 (Wed) 11:10", [
        (U, "How do dentists and orthodontists differ? A friend's kid needs braces."),
        (A, "A dentist handles general oral health; an orthodontist is a specialist in alignment, braces and aligners, with extra years of training. Your friend would need a referral to an orthodontist."),
        (U, "Got it."),
        (A, "Most orthodontists offer a free first consultation."),
    ]),
    ("f04", "2024/02/01 (Thu) 09:00", [
        (U, "Explain the difference between a Roth IRA and a traditional IRA in two sentences."),
        (A, "A traditional IRA gives a tax deduction now and is taxed on withdrawal; a Roth is funded with after-tax money and withdrawals in retirement are tax-free. Which is better depends on whether you expect a higher tax rate now or later."),
        (U, "Clear."),
        (A, "Income limits apply to Roth contributions."),
    ]),
    ("f05", "2024/02/06 (Tue) 21:15", [
        (U, "A coworker is buying a treadmill for about $900. Are they worth it?"),
        (A, "Only if it removes a real barrier, such as weather or childcare. Many end up unused; a used one for half the price is a safer bet."),
        (U, "I'll pass that on."),
        (A, "Check the motor warranty; it is the part that fails."),
    ]),
    ("f06", "2024/02/13 (Tue) 13:40", [
        (U, "What's the best way to clean a cast iron pan?"),
        (A, "Rinse while warm, scrub with coarse salt or a chainmail scrubber, dry on the stove and wipe with a thin coat of oil. Soap in small amounts is fine."),
        (U, "Good to know."),
        (A, "Never leave it to soak."),
    ]),
    ("f07", "2024/02/19 (Mon) 08:30", [
        (U, "Can you give me a pizza dough recipe for a home oven?"),
        (A, "Use 500 g flour, 325 g water, which is 65% hydration, 10 g salt and 2 g instant yeast. Ferment 24 hours in the fridge and bake on a preheated steel at the oven's highest temperature."),
        (U, "Will try it on Friday."),
        (A, "Take the dough out two hours before shaping."),
    ]),
    ("f08", "2024/02/22 (Thu) 15:25", [
        (U, "My neighbour has two parrots and they are loud. Any polite way to bring it up?"),
        (A, "Mention a specific time window rather than the birds in general, and ask whether they could be moved away from the shared wall in the early morning."),
        (U, "Okay."),
        (A, "Most bird owners are aware and appreciate a friendly approach."),
    ]),
    ("f09", "2024/02/27 (Tue) 10:45", [
        (U, "What's a good structure for a weekly team update email?"),
        (A, "Three sections: what shipped, what is blocked and who can unblock it, and what is next. Keep it under 150 words and put asks at the top."),
        (U, "Simple. I like it."),
        (A, "Consistency matters more than polish."),
    ]),
    ("f10", "2024/03/03 (Sun) 18:10", [
        (U, "My brother-in-law's kid is starting violin. Is renting an instrument better than buying?"),
        (A, "Rent. Children move through fractional sizes quickly, and most shops credit rental fees toward a purchase later."),
        (U, "Makes sense."),
        (A, "Ask the teacher to check the setup before the first lesson."),
    ]),
    ("f11", "2024/03/07 (Thu) 11:50", [
        (U, "How does compound interest work, quickly?"),
        (A, "You earn interest on previous interest as well as on the principal, so growth accelerates over time. At 7 percent, money doubles roughly every ten years."),
        (U, "Thanks."),
        (A, "The rule of 72 gives a quick estimate: 72 divided by the rate."),
    ]),
    ("f12", "2024/03/11 (Mon) 14:35", [
        (U, "What should I know before visiting a city with lots of hills on foot?"),
        (A, "Wear shoes with grip, plan routes that go up by transit and down on foot, and carry water. Trams and funiculars are often part of the normal transit pass."),
        (U, "Good tips."),
        (A, "Cobblestones are slippery when wet."),
    ]),
    ("f13", "2024/03/15 (Fri) 09:20", [
        (U, "Summarize the plot of Project Hail Mary without spoilers, a friend lent it to me and I haven't started."),
        (A, "A lone astronaut wakes up with no memory on a ship far from Earth and has to work out both who he is and how to solve a problem threatening the sun. It is a puzzle-driven survival story with a lot of humour."),
        (U, "Sounds fun."),
        (A, "It is a quick read once it gets going."),
    ]),
    ("f14", "2024/03/19 (Tue) 20:00", [
        (U, "Is it normal for a cat to sleep 16 hours a day?"),
        (A, "Yes. Adult cats sleep 12 to 16 hours, more in winter. Worry only if it comes with appetite loss or hiding."),
        (U, "Phew."),
        (A, "Play sessions before meals help them settle at night."),
    ]),
    ("f15", "2024/03/23 (Sat) 12:00", [
        (U, "What's a fair price for a used road bike from 2019?"),
        (A, "Roughly 50 to 60 percent of the original price if it is in good condition, less if the drivetrain needs replacing. Check the frame for cracks near the welds."),
        (U, "It's for a friend who's shopping."),
        (A, "Ask for the service history."),
    ]),
    ("f16", "2024/03/27 (Wed) 08:15", [
        (U, "Give me a quick stretching routine for after sitting all day."),
        (A, "Hip flexor lunge, doorway chest stretch, seated figure four and a standing forward fold, 45 seconds each side. Do it once mid-afternoon and once after work."),
        (U, "Will try."),
        (A, "Standing up every half hour matters more than any routine."),
    ]),
]


def num(*words: str) -> str:
    return r"\b(" + "|".join(words) + r")\b"


# (id, 桶, 题面, 金标文字, rubric 或 None——None 表示走评委)
QUESTIONS: list[tuple[str, str, str, str, dict | None]] = [
    # ---- det-u
    ("det-u-1", "det-u", "What is the name of my dentist?", "Dr. Okafor", {"all_of": ["okafor"]}),
    ("det-u-2", "det-u", "What model year is my current car?", "2017 (Subaru Outback)", {"all_of": ["2017"]}),
    ("det-u-3", "det-u", "What is my gym locker code?", "4182", {"all_of": ["4182"]}),
    ("det-u-4", "det-u", "Which city does my sister live in?", "Leeds", {"all_of": ["leeds"]}),
    ("det-u-5", "det-u", "How much did I pay for my standing desk?", "$389", {"all_of": ["389"]}),
    ("det-u-6", "det-u", "What model is my wifi router?", "Asus RT-AX58U", {"all_of": ["ax58u"]}),
    ("det-u-7", "det-u", "Which nut am I allergic to?", "Cashews", {"all_of": ["cashew"]}),
    # ---- det-a
    ("det-a-1", "det-a", "What hydration percentage did you recommend for my sourdough?", "72%", {"all_of": [r"\b72"]}),
    ("det-a-2", "det-a", "Which book did you tell me to start with for learning SQL?", "Learning SQL by Alan Beaulieu", {"any_of": ["learning sql", "beaulieu"]}),
    ("det-a-3", "det-a", "What rsync flags did you give me for backing up my photos?", "-avz --delete", {"all_of": ["-avz", "--delete"]}),
    ("det-a-4", "det-a", "When did you say I should repot my monstera?", "Late April", {"all_of": ["april"]}),
    ("det-a-5", "det-a", "What monthly payment did you calculate for the car loan?", "About $411.16", {"all_of": [r"\b411"]}),
    ("det-a-6", "det-a", "How long is the hiking trail you recommended?", "7.4 miles (Cedar Ridge Loop)", {"all_of": [r"7\.4"]}),
    # ---- upd
    ("upd-1", "upd", "Where do I work now?", "Novacore Health", {"all_of": ["novacore"]}),
    ("upd-2", "upd", "How much is my rent per month currently?", "$1,525", {"all_of": [r"1,?525"]}),
    ("upd-3", "upd", "How many miles a week am I running these days?", "22", {"all_of": [r"\b22\b"]}),
    ("upd-4", "upd", "What phone do I use now?", "iPhone 15", {"all_of": ["iphone"]}),
    ("upd-5", "upd", "On which day of the week does my book club meet now?", "Thursday", {"all_of": ["thursday"], "none_of": [r"^\W*tuesday"]}),
    ("upd-6", "upd", "Where did I work before my current job?", "Brightline Logistics", {"all_of": ["brightline"]}),
    ("upd-7", "upd", "Who is my daughter's piano teacher now?", "Mr. Chen", {"all_of": ["chen"]}),
    # ---- tmp
    ("tmp-1", "tmp", "On what date did I adopt my cat Miso?", "February 11, 2024", {"any_of": [r"feb\w*\.?\s+11", r"11(th)?\s+(of\s+)?feb", r"02-11", r"\b2/11"]}),
    ("tmp-2", "tmp", "On what date did I run the half marathon?", "March 2, 2024", {"any_of": [r"mar\w*\.?\s+2\b", r"\b2(nd)?\s+(of\s+)?mar", r"03-02", r"\b3/2\b"]}),
    ("tmp-3", "tmp", "How many days passed between the day I adopted Miso and the day I ran the half marathon?", "20 days", {"any_of": [num("20", "twenty")]}),
    ("tmp-4", "tmp", "How many days before adopting Miso did I start my pottery class?", "6 days (class started February 5)", {"any_of": [num("6", "six")]}),
    ("tmp-5", "tmp", "Today is March 30, 2024. How many days ago did my renewed passport arrive?", "12 days", {"any_of": [num("12", "twelve")]}),
    ("tmp-6", "tmp", "On what date is my flight to Lisbon?", "April 10, 2024", {"any_of": [r"apr\w*\.?\s+10", r"10(th)?\s+(of\s+)?apr", r"04-10", r"\b4/10"]}),
    ("tmp-7", "tmp", "In which month and year did I start learning Portuguese?", "November 2023", {"all_of": ["nov", "2023"]}),
    # ---- agg
    ("agg-1", "agg", "How many houseplants do I own right now?", "3 (monstera, snake plant, pothos; the fiddle-leaf fig was not bought)", {"any_of": [num("3", "three")]}),
    ("agg-2", "agg", "How many concerts have I attended so far in 2024?", "3 (Khruangbin, the jazz trio, Mitski)", {"any_of": [num("3", "three")]}),
    ("agg-3", "agg", "How much have I spent in total on bike upgrades this year?", "$180 (tires 84 + saddle 59 + lights 37)", {"all_of": [r"\b180\b"]}),
    ("agg-4", "agg", "Which countries did I travel to last year?", "Portugal, Japan and Mexico", {"all_of": ["portugal", "japan", "mexico"]}),
    ("agg-5", "agg", "How many pets do I currently have?", "2 (Miso the cat and Biscuit the dog; the foster kitten went back)", {"any_of": [num("2", "two")]}),
    ("agg-6", "agg", "How many books have I finished reading this year?", "3 (Piranesi; Tomorrow, and Tomorrow, and Tomorrow; The Overstory)", {"any_of": [num("3", "three")]}),
    ("agg-7", "agg", "Which languages do I speak or study?", "English, Hindi and Portuguese", {"all_of": ["hindi", "portuguese"]}),
    # ---- pref（评委）
    ("pref-1", "pref", "Can you suggest a dinner I could cook tonight?",
     "The user would prefer a vegetarian dinner that contains no mushrooms. A suggestion with meat or fish, or one built around mushrooms, does not fit.", None),
    ("pref-2", "pref", "Any tips for choosing my seat and departure time for the Lisbon flight?",
     "The user would prefer advice that reflects their stated habits: a morning departure and an aisle seat because of their bad knee.", None),
    ("pref-3", "pref", "How should I organise my notes from pottery class?",
     "The user would prefer a suggestion that uses Notion (for example a database or template), since they keep all notes in Notion and paper planners do not work for them.", None),
    ("pref-4", "pref", "How can I save up some extra money before my trip?",
     "The user would prefer advice that builds on their existing budgeting in YNAB (for example a dedicated category or moving money between categories), reflecting that they are budget-conscious.", None),
    ("pref-5", "pref", "What novel should I read next?",
     "The user would prefer a literary fiction or science fiction recommendation, not a thriller, and not a book they already finished (Piranesi; Tomorrow, and Tomorrow, and Tomorrow; The Overstory).", None),
    ("pref-6", "pref", "Can you suggest some cross-training to go with my running?",
     "The user would prefer low-impact options that are careful with their bad left knee (for example swimming, cycling, strength work for hips), ideally fitting a morning routine.", None),
    # ---- abs（评委）
    ("abs-1", "abs", "What is the name of my orthodontist?",
     "The information provided is not enough. You mentioned your dentist, Dr. Okafor, but never an orthodontist.", None),
    ("abs-2", "abs", "How much did I pay for my treadmill?",
     "The information provided is not enough. You mentioned buying a standing desk, but not a treadmill.", None),
    ("abs-3", "abs", "Which city does my brother live in?",
     "The information provided is not enough. You mentioned your sister Priya lives in Leeds, but nothing about a brother.", None),
    ("abs-4", "abs", "On what date did I adopt my parrot?",
     "The information provided is not enough. You adopted a cat, Miso; you never mentioned a parrot of your own.", None),
    ("abs-5", "abs", "What hydration did you recommend for my pizza dough when I told you about my dense crust?",
     "The information provided is not enough. The dense-result conversation was about sourdough bread (72% hydration); the user never reported a dense pizza crust.", None),
    ("abs-6", "abs", "Who is my daughter's violin teacher?",
     "The information provided is not enough. Your daughter takes piano lessons (now with Mr. Chen); no violin teacher was mentioned.", None),
]

# 第 0 层（不调 LLM）用的证据标记：载荷里必须出现这些小写子串，题目才"答得出"。
# 证据没到场是检索/打包的问题，到场了还答错才是呈现或答题器的问题——两类问题分开查。
EVIDENCE: dict[str, list[str]] = {
    "det-u-1": ["okafor"], "det-u-2": ["2017 subaru"], "det-u-3": ["4182"], "det-u-4": ["leeds"],
    "det-u-5": ["$389"], "det-u-6": ["rt-ax58u"], "det-u-7": ["cashews"],
    "det-a-1": ["72% hydration"], "det-a-2": ["beaulieu"], "det-a-3": ["rsync -avz --delete"],
    "det-a-4": ["late april"], "det-a-5": ["411.16"], "det-a-6": ["7.4 miles"],
    "upd-1": ["novacore"], "upd-2": ["1,525"], "upd-3": ["22 miles"], "upd-4": ["iphone 15"],
    "upd-5": ["thursdays"], "upd-6": ["brightline"], "upd-7": ["mr. chen"],
    "tmp-1": ["adopted a cat yesterday"], "tmp-2": ["half marathon last saturday"],
    "tmp-3": ["adopted a cat yesterday", "half marathon last saturday"],
    "tmp-4": ["adopted a cat yesterday", "pottery class three weeks ago"],
    "tmp-5": ["passport arrived in the mail today"], "tmp-6": ["exactly three weeks"],
    "tmp-7": ["two months ago i started learning portuguese"],
    "agg-1": ["bought a monstera", "snake plant today", "added a pothos"],
    "agg-2": ["khruangbin last night", "jazz trio", "mitski was incredible"],
    "agg-3": ["$84", "$59", "$37"],
    "agg-4": ["went to portugal", "japan trip", "mexico trip"],
    "agg-5": ["her name is miso", "my dog biscuit"],
    "agg-6": ["finished piranesi", "finished tomorrow", "finished the overstory"],
    "agg-7": ["hindi and english", "learning portuguese"],
    "pref-1": ["vegetarian"], "pref-2": ["aisle seat"], "pref-3": ["notion"], "pref-4": ["ynab"],
    "pref-5": ["dislike thrillers"], "pref-6": ["left knee"],
}


def main() -> None:
    sessions = sorted(SESSIONS + FILLERS, key=lambda s: s[1])
    evidence_ids = [s[0] for s in SESSIONS]
    out = []
    for qid, bucket, question, gold, rubric in QUESTIONS:
        out.append({
            "question_id": f"{qid}_abs" if bucket == "abs" else qid,
            "question_type": bucket,
            "question": question,
            "answer": gold,
            "rubric": rubric,
            "evidence": EVIDENCE.get(qid, []),
            "question_date": QUESTION_DATE,
            "haystack_session_ids": [s[0] for s in sessions],
            "haystack_dates": [s[1] for s in sessions],
            "haystack_sessions": [[{"role": r, "content": c} for r, c in s[2]] for s in sessions],
            # 共享历史下不逐题标证据会话；证据召回在这套题上不是被测量
            "answer_session_ids": evidence_ids,
        })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_msgs = sum(len(s[2]) for s in sessions)
    print(f"{len(out)} questions, {len(sessions)} sessions, {n_msgs} messages -> {OUT}")


if __name__ == "__main__":
    main()
