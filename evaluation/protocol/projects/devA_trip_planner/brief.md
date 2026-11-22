# Development project A: a personal trip planner (the founder's brief)

Written 2 November 2026 for the evaluation protocol (evaluation/protocol/EVAL_PROTOCOL.md). Public project. The text below the rule is the idea the start command sends, word for word (the Start screen's "idea as typed"; `python -m app.runner.cli start --brief` takes the text after the last `---` line); it follows the trip-planner example on the published website (docs/design/website, the "personal travel planner" workspace: 100 itinerary requests a month, an app budget of 50 USD a month, routes that respect opening hours, encrypted storage, trip data kept out of model training).

---

I want to build a personal travel planner for people who plan their own city trips. A traveller types the places she wants to see, the days she has and how she likes to move around. The planner turns that into a day-by-day itinerary: a route between the stops that respects their opening hours and visit durations, a short written summary of each day, and a map she can open on her phone. She can edit the plan, save it and come back to it on the next trip.

The first version is a web app that works in a phone browser. It needs three outside services: a routing and places service that knows opening hours and computes routes between several stops; a language model that writes the day summaries and answers questions about the plan; and a place to store the saved itineraries and the traveller's notes. The app itself runs on a small hosted service.

Two things are not negotiable. Trip data stays private: where a traveller goes, when, and what she writes about it must not be used by any vendor to train its models or for anything beyond providing the service to us. Saved itineraries and backups are encrypted at rest. The running cost of the app, all services together, stays inside 50 US dollars a month at the pilot's size: about one hundred itinerary requests a month from about one hundred travellers, each itinerary a few routing calls and one or two summaries. This is the app budget, not the traveller's trip budget.

I am one developer. I would rather use well-documented commercial services than run my own servers, and I need a plan I can build in a few weeks. For every service, tell me what to use, what it costs at this size, and what evidence shows it keeps the trip data private.
