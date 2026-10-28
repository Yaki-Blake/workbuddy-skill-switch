# skill-switch

English | [简体中文](README.md)

This script lets you build your own skill lists, turn them on or off in one click, and save some of the thinking time and token consumption.

---

**Contents**

- [1. How much this saves](#1-how-much-this-saves)
- [2. Getting it running](#2-getting-it-running)
- [3. A few concrete details](#3-a-few-concrete-details)
- [4. If you want to change it](#4-if-you-want-to-change-it)
- [5. What it cannot do](#5-what-it-cannot-do)

---

## 1. How much this saves

Work this number out first, or it is easy to expect the wrong thing from the tool.

WorkBuddy joins every enabled skill into a `name: description` line and puts it into the system prompt. One skill is roughly 125 tokens, and the total adds up linearly as you install more.

So the standing cost is about the number of injected skills times 125, and dividing that by the average input per request gives the share it takes.

I ran it on this machine a moment ago.

| Item | Value |
| --- | --- |
| Skills installed | 149 (20 of them declare `disable-model-invocation` and never enter the context) |
| Standing cost actually injected | ~16,082 tokens |
| Average input per request | ~1,173,000 tokens (737 million divided by 628 requests) |
| Cache hit rate | 97.4% |
| Share | 1.37% |

That 125 comes from dividing 16,082 by the 129 skills that actually make it into the context, which gives 124.6. Descriptions run long and short, and measured values for a single skill fall between 80 and 250, so the coefficient is only good for an order of magnitude.

1.37%. Cutting the skills in half brings it to 0.68%. Once I saw that figure I stopped expecting the tool to save me money.

So what is left.

The most direct gain is that the model picks the wrong tool less often. With more than a hundred skills laid out flat in the system prompt it occasionally reaches for something unrelated, and a shorter list makes that noticeably rarer. That one is a matter of feel; I cannot give you a precise number.

The other point is more concrete. I can look things up now. How many tokens each skill takes, who changed it to what state and when, all of it is one look away. Before, I would change something and forget it; now at least something records it.

If you really want to know where the money goes, open the usage page in the interface and sort by session. The most expensive conversations are usually ones where a large file got resent over and over, and that is where the bulk sits.

## 2. Getting it running

Double-click `启动界面.bat`. The browser opens `http://127.0.0.1:8899` by itself. The left column holds the lists, the right one holds the skills, and the search box above them takes focus when you press slash. On the right there are also buttons for suggested picks and sorting, and along the bottom sit the selected count and the place to save.

### Step 1, pick out what you do not use

Each skill takes one row on the right with a checkbox in front. The easiest route is the suggested-picks button at the top. It groups skills by name prefix, one family starting with `ardot-`, another with `workbuddy-`, so a large block can be selected at once. To pick more carefully, search, or just click rows.

While you select, watch the line at the bottom. It follows along, reading something like "Selected 12 · ~1.9k tok". That number is the context this batch of skills takes up right now, and the amount you would save by turning them off.

When you are done, type a name in the field below, something like "unused", and click Save list. A card appears on the left showing how many members the list has and what state each one is in.

Building a list writes no configuration at all, so you can do it any time, even with WorkBuddy running.

### Step 2, close WorkBuddy completely

Quit from the tray icon. Clicking the X in the corner of the window does not count. While the app is alive it holds a copy of the configuration in memory, and at the moment it exits it flushes those old values back to disk, overwriting whatever was changed from outside.

### Step 3, click Close on the card

That click is what writes `settings.json`.

What the interface calls Close is really dormancy. The model stops reaching for the skill on its own and the context cost goes away, but you can still call it up any time with a slash and its name. That is the one I use. Disabling a skill outright takes `--hard` on the command line.

The three states line up like this.

| What you do | State | Model can call it | You can call it |
| --- | --- | --- | --- |
| Click Enable | default | yes | yes |
| Click Close | dormant | no | yes |
| Add `--hard` on the command line | off | no | no |

Afterwards the state breakdown on the card changes, and the activity log gains an entry.

### Step 4, restart WorkBuddy

The configuration is read into memory at startup, so nothing takes effect without a restart. There is no way around this; it comes down to when the file is read.

### Step 5, look at the result

Back in the interface, the card shows how many members sit in each state. To undo it, click Enable on the same card and restart once more.

From then on daily use is two steps. Open the interface for a look, click Enable or Close when you need to. A list built once does not need to be touched again.

## 3. A few concrete details

### The numbers on screen

The `~470` on the right of a row is the estimated share of the system prompt that skill takes, counting only the name and description. The tilde marks it as an estimate, and red means it went past 250 tokens.

Some rows show a dash instead. Those skills declare `disable-model-invocation` themselves and never enter the context, so turning them off accomplishes nothing. That column keeps you from doing work with no payoff.

Hovering over any row for half a second pops up the body, `SKILL.md`, or `README.md` if that is missing, with the line count and body tokens underneath. That number and the one in the list are two different bills. The list shows what it costs to keep the skill around; the popup shows what it costs to actually call it.

The line at the bottom, "Showing 128 / 128 · injected ~16,082 tok", gives the filtered count and the selectable total. Only user-level skills are listed by default; built-in and plugin skills appear once you turn on show-all. The total on the right always covers what really enters the system prompt, so it is larger than what the `cost` command counts by default.

The usage page puts total credits, input and output tokens, and cache hit rate up top. Below are two tables, switched by the Recent calls and By skill buttons, with Recent calls as the default.

By skill only counts explicit `/skill-name` calls, which gives very low coverage; the reason is in section 5. Total credits and per-session figures are accurate. Also, when one turn calls several skills, that turn's cost is counted once under each of them, so this table's total runs larger than the skill-round credits in the top right corner. That is not an arithmetic error.

### Command line

The interface covers daily use. This section is for scripting and for your own debugging.

```bash
python skillset.py ls                     # all lists and their state breakdown
python skillset.py cost --top 20          # sort by cost, see what to close first
python skillset.py off coding --dry-run   # dry run, write nothing
python skillset.py off coding             # put the whole list to sleep
python skillset.py off coding --hard      # disable it outright
python skillset.py on coding              # restore
python skillset.py status                 # what was changed, and by whom
python skillset.py restore                # restore from the latest backup
```

All thirteen subcommands.

| Command | What it does |
| --- | --- |
| `ls` | list every list, with state breakdown and savable tokens |
| `cost [--top N] [--all]` | sort by cost, `--all` includes built-in and plugin skills |
| `show <list>` | members, states, and who changed them last |
| `new <list> [members...]` | create a list |
| `pick <list> [--append] [--numbers]` | interactive picker in the terminal, `--numbers` for a numbered mode |
| `add` / `rm <list> <members...>` | add or remove members |
| `del <list>` | delete a list, leaving the states already written alone |
| `on` / `off <list>` | restore or put to sleep; `off` with `--hard` disables outright |
| `status` | which skills were changed, and where from |
| `log` | the last 20 operations |
| `restore` | restore configuration from the latest backup |

Three general flags. `--dry-run` shows without writing, supported by `on` and `off`. `--force` writes while the app is running, and also allows operating on built-in skills. `--config-dir X` points at another config directory and must come before the subcommand.

### Stuck

**It says Python cannot be found.** The script tries `python` first, then `py -3`. If both fail, Python is not on PATH. Reinstall and tick Add Python to PATH.

**Port 8899 is taken.** It walks forward by itself, trying up to 12 ports. Read the actual address from what the window prints.

**Clicking Close says the app is running.** Quit properly as in step 2, tray icon included.

**Closed it but nothing looks different.** Check whether you restarted first. If you did and it still looks the same, open the activity log and see whether another list's operation overwrote this skill. When a skill belongs to two lists at once, the later action wins.

## 4. If you want to change it

All of it is why I chose things at the time, kept for my own future reading.

### One write path

```
browser ui.html  --HTTP-->  skillui.py  --calls-->  skillset.py  --writes-->  settings.json
                                                          ^
                                     terminal skillset.py --+
```

There is no code in `skillui.py` that writes to disk. Each endpoint tidies up its arguments, hands them to `skillset.apply_to_settings()`, then captures that function's printed output and exit code and turns them into an HTTP response. The interface and the command line take the same road. That is deliberate; two implementations drift apart sooner or later.

`skillusage.py` sits on another line entirely, reading session records and taking no part in writes.

Data lands in three JSON files. `lists.json` remembers who belongs with whom, `ledger.json` remembers who changed what and when, and `settings.json` is the one that currently takes effect. Deleting the first two does not affect skill states; they are only records.

| Problem | The choice | The cost |
| --- | --- | --- |
| Knowing whether the app is running | `tasklist` plus a reverse lookup of the process name | Only works on Windows, and a different app name slips through |
| Not corrupting the configuration | Backup, temp file, atomic replace, then a byte-by-byte comparison | Two extra disk round trips per write, tens of milliseconds on a large config |
| Estimating tokens | A made-up coefficient, 1.5 per Chinese character, 3.6 characters per token otherwise | Not exact, comparisons only. Accuracy would mean pulling in a third-party tokenizer and losing the zero-dependency property |
| Drawing the terminal UI | Raw ANSI escape sequences repainted by hand, arrow keys read through `msvcrt` | Only works on Windows, falls back to numbered input when virtual terminal support fails |
| How much the hover panel shows | The first 1800 characters, and the mouse cannot enter the panel | Long files get cut off. In exchange the panel does not flicker when the cursor leaves the row |
| Attributing usage to skills | Only the session record where `name` equals `Skill` | Coverage is very low, around 2% |

The last one deserves a couple more lines. It could be made more accurate. Check whether the assistant read a given `SKILL.md` in a turn and you can infer which skill it used, with coverage approaching one hundred percent. I did not do it. That would be inference; the session record contains no such line. A number whose provenance I cannot explain is worse than an honest note saying only explicit calls are counted.

## 5. What it cannot do

It runs on Windows only. `tasklist` and `msvcrt` are Windows-only, so on Mac and Linux the configuration has to be edited by hand.

The app must be closed before writing configuration. The configuration is read into memory at startup and flushed back on exit, and the tool blocks the write. `--force` does not change that fact.

It only touches user-level skills. Only what sits under `~/.workbuddy/skills/` is under its control; built-in skills and ones installed by plugins are locked by default.

Per-skill usage is essentially inaccurate. Only explicit `/skill-name` calls leave a trace, measured at about 2% coverage. Total credits and per-session figures are accurate.

### How to tell it still works

This tool fails silently. If WorkBuddy ever changes what a field means or how the skill cache is shaped, the tool keeps writing, keeps reporting no error, and simply does not take effect. So do not trust that it worked last time. Check every so often; it takes thirty seconds.

Pick any skill, click Close, restart WorkBuddy, then see whether it still appears in the skill list. Gone means things are fine. Still there means opening the activity log first and checking whether this change was recorded. Recorded but not applied means the field's meaning changed. Not recorded at all means the write was blocked.
