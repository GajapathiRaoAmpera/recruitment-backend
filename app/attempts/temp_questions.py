"""TEMPORARY aptitude questions so the exam can be tried before the question bank is linked.

Remove this file and its use in service.py once the paper builder / question bank writes
`test_questions` for a test (PUT /tests/{id}/questions). They are only attached to a test
that has no questions of its own, and never when APP_ENV=prod. Tagged `temp-seed` so they
are easy to find and delete: delete from questions where 'temp-seed' = any(tags).
"""

import uuid

TAG = "temp-seed"
_NS = uuid.UUID("5d0d7e0e-6f3a-4b7e-9a51-0c1f2b3a4d5e")

# (question, options, index of the correct option)
_RAW = [
    ("What comes next in the series 2, 6, 12, 20, 30, ?", ["40", "42", "44", "46"], 1),
    (
        "A train 120 m long passes a pole in 6 s. Its speed is:",
        ["60 km/h", "72 km/h", "80 km/h", "90 km/h"],
        1,
    ),
    ("If 20% of a number is 45, the number is:", ["180", "200", "225", "250"], 2),
    ("Find the odd one out: 16, 25, 36, 49, 58", ["16", "36", "49", "58"], 3),
    (
        "A can do a job in 10 days, B in 15 days. Together they finish it in:",
        ["5 days", "6 days", "8 days", "12 days"],
        1,
    ),
    (
        "Which time complexity is best for binary search on a sorted array?",
        ["O(n)", "O(log n)", "O(n log n)", "O(1)"],
        1,
    ),
]

QUESTIONS = [
    {
        "id": uuid.uuid5(_NS, body),
        "body": body,
        "options": options,
        "answer": answer,
    }
    for body, options, answer in _RAW
]
