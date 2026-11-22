# Development project B: a Hebrew support assistant for a small shop (the founder's brief)

Written 2 November 2026 for the evaluation protocol (evaluation/protocol/EVAL_PROTOCOL.md). Synthetic project: the shop, its figures and its founder are invented for the evaluation; no real business is described. The text below the rule is the idea the start command sends, word for word (`python -m app.runner.cli start --brief` takes the text after the last `---` line).

---

I run a small online shop that sells kitchenware to customers in Israel. Most customer questions arrive in Hebrew, on the website's chat and by email: where is my order, can I return this pan, does this pot work on induction, do you deliver to Eilat. Two of us answer them between packing orders, and evenings and weekends go unanswered. I want a support assistant that answers the routine questions itself, in Hebrew, from our product pages, our delivery and returns policy and the order status, and hands anything unusual to one of us with the conversation attached.

The assistant has to work in both channels. On the website it sits in a chat widget that customers see in Hebrew, laid out right to left like the rest of the site. By email it reads the customer's message and sends the reply from our own domain, in Hebrew, so that a reply looks like it comes from the shop and not from a tool. Customers also write in English now and then, and the assistant may answer those in English, but Hebrew is the requirement, not a nice-to-have.

The shop is small and so is the money. All the services the assistant needs, together, must cost at most 40 US dollars a month at our volume: about twenty conversations a day, a few hundred emails a month, two staff accounts. I will not pay for a seat we do not use, and I would rather start on free plans and upgrade when the volume asks for it.

I need three decisions: which language model answers in Hebrew; which chat widget shows the conversation in Hebrew on the site; and which email service sends the replies from our domain in Hebrew. For each one I want the evidence that it handles Hebrew, what it costs at our volume, and what would make you change your mind.
