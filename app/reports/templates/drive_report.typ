// Drive report. All data arrives as JSON in sys.inputs.data, never as markup, so candidate text
// can't run code. Shape: app/reports/service.py build().
#let d = json(bytes(sys.inputs.data))

// Ampera tokens, copied from recruitment-frontend/src/styles/_tokens.scss
#let ink = rgb("#050505")
#let muted = rgb("#5b5b5b")
#let line-c = rgb("#e4dfe8")
#let brand = rgb("#9c369a")     // magenta-600, the logo "A"
#let lavender = rgb("#8070b4")  // logo wordmark
#let navy = rgb("#000f5c")
#let soft = rgb("#f7edf7")      // magenta-50

#set document(title: d.drive.company + " campus drive report")
#set page(
  paper: "a4", margin: (x: 2cm, top: 2.2cm, bottom: 2cm),
  header: context if counter(page).get().first() > 1 [
    #set text(8pt, fill: muted)
    #box(baseline: 25%, image("ampera-logo.png", height: 11pt)) #h(8pt) #d.drive.company · #d.drive.role #h(1fr) #d.report_id · Confidential
    #v(-6pt) #line(length: 100%, stroke: 0.5pt + line-c)
  ],
  footer: context [
    #set text(8pt, fill: muted)
    Generated #d.generated_at by #d.generated_by #h(1fr) Page #counter(page).display() of #counter(page).final().first()
  ],
)
// shortcut: system sans fonts; bundle the brand font (Switzer) and pass font_paths for pixel-identical output
#set text(font: ("Switzer", "Inter", "Liberation Sans", "DejaVu Sans"), size: 10pt, fill: ink)
#set par(justify: false, leading: 0.6em)
#show heading.where(level: 1): it => block(above: 14pt, below: 8pt)[#box(width: 4pt, height: 13pt, fill: brand, baseline: 15%) #h(4pt) #text(15pt, weight: "bold", fill: navy, it.body)]
#show heading.where(level: 2): it => block(above: 12pt, below: 6pt, text(11.5pt, weight: "bold", fill: navy, it.body))
#set table(stroke: (x, y) => (bottom: 0.5pt + line-c), inset: (x: 6pt, y: 5pt))
#show table.cell.where(y: 0): set text(weight: "bold", fill: muted, size: 8.5pt)

#let stat(label, value) = box(width: 100%, fill: soft, radius: 4pt, inset: 10pt)[
  #text(18pt, weight: "bold", fill: navy, value) \ #text(8.5pt, fill: muted, label)
]
#let hbar(frac) = box(width: 100%, height: 9pt, fill: soft, radius: 2pt)[
  #box(width: frac * 100%, height: 9pt, fill: brand, radius: 2pt)
]
#let none-row(cols, msg) = table.cell(colspan: cols, text(fill: muted, msg))

// ---------------------------------------------------------------- cover
#image("ampera-logo.png", height: 1.1cm)
#v(2.2cm)
#box(width: 3cm, height: 4pt, fill: brand)
#v(10pt)
#text(10pt, weight: "bold", fill: lavender, tracking: 1.5pt)[CAMPUS RECRUITMENT DRIVE REPORT]
#v(4pt)
#text(28pt, weight: "bold", fill: navy, d.drive.role)
#v(2pt)
#text(14pt, fill: muted, d.drive.company + " · " + d.drive.college)
#v(1.2cm)
#grid(columns: (auto, 1fr), row-gutter: 8pt, column-gutter: 16pt,
  text(fill: muted)[Drive dates], [#d.drive.start – #d.drive.end],
  text(fill: muted)[HR owner], d.drive.hr_owner,
  text(fill: muted)[Report ID], d.report_id,
  text(fill: muted)[Generated], [#d.generated_at by #d.generated_by],
)
#v(1fr)
#box(width: 100%, fill: soft, radius: 4pt, inset: 12pt)[
  #text(9pt, fill: muted)[*Confidential.* For internal documentation. Contains candidate results; do not forward outside the organisation. Personal details (date of birth, phone, email), test answers and interview recordings are deliberately excluded.]
]
#pagebreak()

// ---------------------------------------------------------------- summary
= Executive summary
#grid(columns: (1fr,) * d.headline.len(), column-gutter: 8pt, ..d.headline.map(s => stat(s.label, s.value)))
#v(10pt)
The drive received *#d.funnel.first().count applications* and closed with *#d.funnel.last().count selected*. Each stage below shows how many candidates reached it and the share kept from the previous stage.

== Selection funnel
#let top = calc.max(1, d.funnel.first().count)
#table(columns: (10em, 1fr, 3.5em, 4.5em), stroke: none, inset: (x: 4pt, y: 4pt),
  table.header([Stage], [], [Count], [Kept]),
  ..d.funnel.enumerate().map(((i, f)) => {
    let prev = if i == 0 { 0 } else { d.funnel.at(i - 1).count }
    (f.stage, hbar(f.count / top), align(right, str(f.count)),
     align(right, text(fill: muted, if i == 0 or prev == 0 [—] else [#calc.round(100 * f.count / prev, digits: 1)%])))
  }).flatten()
)

== Round overview
#table(columns: (1fr, auto, auto, auto, auto, auto, auto), align: (left, right, right, right, right, right, right),
  table.header([Round], [Cut-off], [Invited], [Sat], [Absent], [Passed], [Avg score]),
  ..d.rounds.map(r => (r.name, r.cutoff, str(r.invited), str(r.sat), str(r.absent), str(r.passed), r.avg)).flatten()
)

// ---------------------------------------------------------------- rounds
#for r in d.rounds [
  #pagebreak(weak: true)
  = #r.name
  #text(fill: muted)[#r.date#if r.duration_min != none [ · #r.duration_min minutes] · cut-off #r.cutoff]
  #v(4pt)
  #grid(columns: (1fr,) * 4, column-gutter: 8pt,
    stat("Sat the round", str(r.sat) + " / " + str(r.invited)),
    stat("Passed", str(r.passed)),
    stat(if r.is_test [Median score] else [Median AI score], r.median),
    stat(if r.is_test [Highest score] else [Highest AI score], r.max),
  )
  #if r.is_test [
    == Score distribution
    #if r.has_scores {
      let mx = calc.max(1, ..r.buckets)
      block(height: 3.4cm, width: 100%, grid(columns: (1fr,) * 10, column-gutter: 5pt, align: bottom,
        ..r.buckets.map(b => align(center + bottom)[
          #text(7.5pt, fill: muted, str(b)) \
          #box(width: 100%, height: (b / mx) * 2.4cm, fill: lavender, radius: (top: 2pt))
        ])))
      grid(columns: (1fr,) * 10, column-gutter: 5pt,
        ..range(10).map(i => align(center, text(7.5pt, fill: muted, str(i * 10) + "–" + str(i * 10 + 10) + "%"))))
    } else [#text(fill: muted)[No scores recorded.]]
    #v(4pt)
    #grid(columns: (1fr, 1fr), column-gutter: 16pt,
      [
        == Top performers
        #table(columns: (1fr, auto, auto), align: (left, left, right),
          table.header([Name], [Student ID], [Score]),
          ..if r.top.len() == 0 { (none-row(3, [No finished attempts.]),) } else { r.top.map(t => (t.name, t.sid, t.score)).flatten() })
      ],
      [
        == Integrity
        #table(columns: (1fr, auto), align: (left, right),
          table.header([Signal], [Count]),
          [Students with tab-switch warnings], str(r.flags.tab_switches),
          [Attempts locked after 3 switches], str(r.flags.locked),
          [Absent], str(r.absent))
      ],
    )
  ] else [
    == AI score and HR decision
    The AI interview score (1–5, average of communication, technical depth, problem solving and culture fit) is advice only. HR made every decision.
    #table(columns: (auto, auto, auto, auto, 1fr), align: (left, left, right, left, left),
      table.header([Name], [Student ID], [AI score], [HR decision], [HR note]),
      ..if r.decisions.len() == 0 { (none-row(5, [No students in this round.]),) } else {
        r.decisions.map(x => (x.name, x.sid, x.ai, text(weight: "bold", x.decision), text(fill: muted, x.note))).flatten()
      })
  ]
]

// ---------------------------------------------------------------- selected
#pagebreak(weak: true)
= Selected candidates
#table(columns: (auto, 1fr, auto) + (auto,) * d.score_heads.len(), align: (x, y) => if x < 3 { left } else { right },
  table.header([\#], [Name], [Student ID], ..d.score_heads.map(h => [#h])),
  ..if d.selected.len() == 0 { (none-row(3 + d.score_heads.len(), [No candidates were selected.]),) } else {
    d.selected.enumerate().map(((i, c)) => (str(i + 1), text(weight: "bold", c.name), c.sid, ..c.scores)).flatten()
  }
)

// ---------------------------------------------------------------- audit + method
= Decision log
#table(columns: (auto, auto, 1fr),
  table.header([When (IST)], [By], [Action]),
  ..if d.log.len() == 0 { (none-row(3, [No recorded actions.]),) } else { d.log.map(a => (a.when, a.who, a.action)).flatten() }
)

= How scores are calculated
#set text(9pt)
- *Aptitude, coding and technical tests:* marks earned as a percentage of the paper's total. The cut-off is chosen by HR when advancing the round; a score equal to the cut-off passes.
- *AI interview:* the AI interviewer rates communication, technical depth, problem solving and culture fit from 1 to 5; the score is their average. It is advice only; HR passes or fails each student.
- *Integrity:* every tab switch during a test is recorded; the third switch locks the attempt.
- *Absent:* invited but did not finish the round before it closed.
