# Lost & found policy - Hotel Aurora

<!--
Copy this to knowledge/lost-and-found-policy.md and replace it with your own
numbers. This file feeds the extract_claim prompt and the return-email
template - keep it factual and short.
-->

## Hold periods

- We hold a claimed, matched item for **90 days** after we tell the guest we
  found it, whether they ask us to ship it or to collect it in person.
- An item nobody ever claims goes to lost-property disposal after **180
  days** (donate in good condition, otherwise dispose).

## Shipping

- Return postage is **free to the guest** - we pay for tracked courier.
- We ship to the address on the guest's reservation unless they give us a
  different one in their reply.
- We do not ship high-value items (see below) without the duty manager's
  sign-off, even after a confident match.

## High-value items - always escalate

These categories always route to the duty manager before anything ships,
however confident the match:

- **Documents**: passports, ID cards, driving licences.
- **Jewellery**: rings, bracelets, necklaces, earrings, watches with
  precious metal or stones.

The duty manager decides whether to verify the guest's identity before
shipping, arrange collection in person instead, or involve the police for a
document. This is not optional and is enforced in the matcher, not just
written here (`tools/engine.py:is_high_value`).

## What we do not do

- We do not guess at a match we are not confident about. An honest "nothing
  similar in the log yet" is always better than shipping the wrong item.
- We do not ship anything before a person has approved the match.
- We do not keep a guest's card or payment details on file for return
  postage - it is free.

## Categories we log

Soft toys, earphones/headphones, jewellery, sunglasses, reading glasses,
identity documents, chargers and cables, e-readers, fleeces/jumpers,
jackets/coats, belts, watches. Anything else still gets logged - the
matcher simply will not force a category match on it.

**Adding a property-specific category (e.g. beach towels).** Listing it here
tells the agent it exists, but the MATCHER only scores a match on a category
it has been given keywords for. List it here for the record, and add it to
`config/agent.yaml`'s `lost_found.extra_categories` with the words a guest
would actually use, for example:

```yaml
lost_found:
  extra_categories:
    - id: beach_towel
      family: beach_gear
      label: "beach towel"
      keywords: ["beach towel", "pool towel"]
```

Without the `config/agent.yaml` entry, a claim in this category will never
score above 0% against a logged item, however word-for-word the description
matches (`make doctor` shows how many custom categories are configured).
