# Kayak Knowledge Files

This guide explains the two indexed files that control knowledge in Kayak:

- `who_knows_me.txt`
- `define_children.txt`

These files live next to `entity.txt` inside an entity folder.

Example:

```text
KayakDB/Template/categories/factions/Holy_Nation/
  entity.txt
  who_knows_me.txt
  define_children.txt
```

Both files are indexed. If you edit them, Kayak needs a reindex before the changes appear in game.

## The short version

Think of the system as three layers:

1. `entity.txt`
What the entity is.

2. `who_knows_me.txt`
Who is allowed to know this entity.

3. `define_children.txt`
What related entities should be pulled into prompt context after this entity is found.

That separation matters.

- Access is about permission.
- Children are about relevance.

An NPC may be allowed to know something, but that does not mean it should always be injected into the prompt.

## Safe defaults

If `who_knows_me.txt` is missing or empty, the entity is unrestricted. All NPCs can read it.

If `define_children.txt` is missing or empty, the entity does not add authored child context.

So an empty file is safe. It means "do nothing special yet."

This is why the Template now includes empty copies of both files for every entity folder. Players can open them and start authoring without creating files by hand.

## `who_knows_me.txt`

`who_knows_me.txt` controls access.

It answers this question:

`Can the current target NPC know this entity?`

Kayak compares the file against the target NPC's merged context. That context can include stored entity fields plus runtime data such as faction, race, town, region, rank, tags, and similar values.

### Rule syntax

Each `[RULE]` block is an AND block.

Multiple `[RULE]` blocks are OR.

Multiple values on one line are OR.

Example:

```text
[RULE]
faction = Holy_Nation
city = Blister_Hill | Stack | Bad_Teeth

[RULE]
faction = Tech_Hunters
race = Greenlander
```

This means:

- Holy Nation members from Blister Hill, Stack, or Bad Teeth can know it.
- Tech Hunters who are Greenlanders can also know it.

### How matching works

Inside one rule block:

- all lines must pass
- a line passes if any value on that line matches

Between rule blocks:

- if any full block passes, access is granted

### Matching is normalized

These are treated as the same:

- `Holy Nation`
- `holy_nation`
- `HOLY_NATION`

So modders can write either spaces or underscores.

### Common keys

You are not limited to one tiny fixed schema, but these are the most useful keys to start with:

- `faction`
- `origin_faction`
- `race`
- `city`
- `town`
- `location`
- `region`
- `rank`
- `role`
- `tags`

### Good patterns

Faction-wide public knowledge:

```text
[RULE]
faction = Anti_Slavers
```

City-local knowledge:

```text
[RULE]
city = Black_Scratch
```

Inner-circle knowledge:

```text
[RULE]
faction = Holy_Nation
rank = Inquisitor | High_Paladin
```

Two different ways to qualify:

```text
[RULE]
faction = Tech_Hunters

[RULE]
race = Skeleton
region = Black_Desert
```

### Bad patterns

Do not use prose or sentence-like values:

```text
[RULE]
faction = people who travel a lot
```

Do not try to encode complex logic into one value:

```text
[RULE]
rank = paladin and priest
```

Write separate conditions instead:

```text
[RULE]
rank = Paladin
tags = priest
```

### Empty file behavior

An empty `who_knows_me.txt` means unrestricted access.

That makes it safe to leave most entities blank until you actually need to gate them.

## `define_children.txt`

`define_children.txt` controls authored context expansion.

It answers this question:

`If this entity is already relevant, what related entities should it pull into the prompt, and in what priority?`

### Syntax

Each line is:

```text
W=<0.00 to 1.00> <EntityName>
```

Example:

```text
W=1.00 Holy_Nation
W=0.80 Blister_Hill
W=0.50 Phoenix_Sword
W=0.10 Greenlander
```

This means:

- `Holy_Nation` is very important nearby context
- `Blister_Hill` is strong nearby context
- `Phoenix_Sword` is moderate
- `Greenlander` is weak background context

### What weights mean

Weights are not permissions.

They are priority.

- `1.00` very strong
- `0.80` strong
- `0.50` medium
- `0.10` weak

### Good patterns

Leader -> faction -> capital:

```text
W=1.00 Holy_Nation
W=0.80 Blister_Hill
```

City -> faction -> region:

```text
W=1.00 Traders_Guild
W=0.70 Great_Desert
W=0.40 Heft
```

Lore entry -> major participants:

```text
W=1.00 Anti_Slavers
W=0.90 Slave_Traders
W=0.70 United_Cities
```

### Weak generic context belongs at low weight

Broad category entities like races, vague world terms, or general cultural background should usually be low weight.

That keeps the prompt focused on the specific thing the player asked about.

### Missing or bad targets

If a child name does not match a real entity, Kayak ignores it and logs a warning.

That means bad lines do not crash the system, but they also do not help.

## A complete worked example

### `entity.txt`

```text
Category = factions
Name = Anti_Slavers
Id = anti_slavers
display_name = Anti-Slavers
leader = Tinfist
capital = Spring

$description = A militant anti-slavery movement led by Tinfist.
```

### `who_knows_me.txt`

```text
[RULE]
faction = Anti_Slavers

[RULE]
faction = Tech_Hunters
region = Stobe_s_Gamble | The_Hook

[RULE]
tags = escaped_slave | abolitionist
```

### `define_children.txt`

```text
W=1.00 Tinfist
W=0.90 Spring
W=0.80 Slave_Traders
W=0.70 United_Cities
W=0.25 Reavers
```

Result:

- An Anti-Slaver can know this entity immediately.
- Some nearby Tech Hunters can know it too.
- Escaped slaves or abolitionist-tagged NPCs can know it even if they are outside those factions.
- When the player asks about the Anti-Slavers, Kayak will prefer nearby context like `Tinfist`, `Spring`, `Slave_Traders`, and `United_Cities`. This is limited by how many children entities your prompt token has set on "with N children".

## How to author these files in practice

### Start small

Do not try to gate everything on day one.

A good first pass is:

- leave most `who_knows_me.txt` files empty
- add restrictions only to secret, local, or faction-sensitive entities
- add `define_children.txt` only to important entities that should pull very specific nearby context

### Gate secrets, not everything

Good first candidates for `who_knows_me.txt`:

- inner doctrine
- hidden bases
- covert factions
- personal histories
- region-specific rumors

Poor first candidates:

- basic cities
- major races
- universally famous leaders
- obvious public factions

### Use `define_children.txt` for focus

Good first candidates for `define_children.txt`:

- faction leaders
- capitals
- famous locations
- big historical events
- faction entities

These are the places where broad automatic field linking usually feels too messy.

## Relationship between the new files and `$knows_about`

`$knows_about` still exists as a legacy compatibility path and bridge-level seed list.

But for authored worldbuilding:

- prefer `who_knows_me.txt` for access
- prefer `define_children.txt` for nearby context

## Debugging

If knowledge feels too restrictive:

- empty `who_knows_me.txt` means unrestricted
- empty `define_children.txt` means no authored expansion
- `EnableKayakKnowledgeFilters = 0` disables knowledge restrictions globally for debugging or sandbox play

If something is not showing up:

1. make sure the target entity name matches a real folder name
2. make sure you reindexed after editing indexed files
3. check whether `who_knows_me.txt` is blocking the target NPC
4. check whether the child entity name in `define_children.txt` matches the real entity

## Reindex reminder

After editing any of these files, Kayak must rebuild its index:

- `entity.txt`
- `who_knows_me.txt`
- `define_children.txt`

Runtime files like `stats.txt` and `dialogue.txt` do not need that.
