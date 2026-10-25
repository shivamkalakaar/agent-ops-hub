# Cheap open models just changed the agent math

*An open-weight model scored 88 on X at $0.20 per million tokens. Here's what that does to the way teams build agents.*

Last week a lab released an open-weight model that scores 88 on X. It costs $0.20 per million tokens to run. I read the [announcement](https://example.com/a) twice, because a year ago that score came with a frontier price tag and a waitlist.

Most coverage stopped at the benchmark. I care more about the price, since price decides what's worth building. When a call is cheap enough, you stop rationing it, and the design of the whole product shifts.

## What actually happened

The lab put out the weights, a short technical report and a price sheet from two hosting partners. The headline number is the 88 on X, which puts it level with models that were state of the art not long ago. The number that matters to me is the $0.20.

At that price you can run an agent that calls the model ten times per task and still spend less than one call to a frontier model used to cost. I've watched teams cut retries, skip self-checks and drop evaluation passes because each one cost real money. Those trade-offs were rational. They're about to look strange.

The report is honest about the gaps, too. The model is weaker on long multi-step tool use and it trails the frontier on the hardest coding tasks. Nobody should read this as "open caught up on everything." It caught up on a lot of the work most products actually do.

## Why the price matters more than the score

Benchmarks tell you what a model can do once. Pricing tells you how many times you can afford to let it try.

Think about a support agent that drafts a reply, checks it against policy, rewrites it and then grades its own tone. That's four calls. On an expensive model, teams collapse it into one call and hope. On a cheap one, you keep all four and the quality goes up because the system checks itself.

The same thing happens in coding agents. A cheap model can propose three patches, run the tests on each and keep the one that passes. You get reliability from volume instead of from a single brilliant answer. I'd take that trade most days, because it's easier to debug and easier to measure.

There's a second effect that gets less attention. When calls are cheap, you can afford to log and evaluate everything. Every run becomes a data point. Teams that were sampling 5% of traffic for review can look at all of it, and that's where the real product improvements come from.

## Where the money is going instead

Cheap inference doesn't mean the industry spends less. It means the spend moves. The same week, a neocloud signed a [$4B GPU deal](https://example.com/b) that runs over five years. Somebody expects a lot more tokens to flow, at lower prices per token, for a long time.

That fits what I see in product teams. Budgets are holding steady and moving from a few expensive calls toward many cheap ones, plus the infrastructure to run evaluations, caching and routing. The bill looks different, not smaller.

It also changes who wins. If raw capability on everyday tasks becomes cheap and common, the advantage shifts to whoever has the best data, the best evaluation loops and the tightest integration into a customer's workflow. The model becomes a component you swap, and the system around it is the product.

## What I'd do this quarter

If you run an AI product, I'd do three things before the next planning cycle.

First, find the places where you rationed calls because of cost. Retries you removed, checks you skipped, evaluations you sampled. Price them again at today's open-model rates. Some of those decisions will flip.

Second, put a router in front of your models if you don't have one. Send the easy, high-volume work to the cheap open model and keep the frontier model for the hard 10%. You'll learn quickly which tasks really need the expensive one, and the answer is usually fewer than you think.

Third, invest in evaluation before you invest in a bigger model. Cheap calls only help if you can tell a good answer from a bad one automatically. A small golden set and a grader you trust will do more for quality than switching to the newest release.

I think within a year most agent workloads run on open models by default, and the frontier labs compete on the hardest slice of the work. For anyone building products that's good news, because the thing you control, the system around the model, ends up mattering more.

I'll keep tracking the prices. They move faster than the benchmarks do, and right now they tell the better story.
