"""Create an idempotent 100-question bank for the NWLST programme."""
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import select, func

load_dotenv()
from database import AsyncSessionLocal  # noqa: E402
from models import TrainingProgram, QuestionBank, Question, QuestionType  # noqa: E402
from question_options import balanced_correct_labels, redistribute_options
BANK_NAME = "NWLST Comprehensive Question Bank (100 Questions)"

BASES = [
    ("Industrial radiography", "What is the safest action before entering an industrial radiography exposure area?", ["Confirm the source is shielded and the area has been surveyed", "Enter immediately after the warning light stops", "Remove the barriers", "Ignore the survey meter"], 0, "Entry requires confirmation that the source is secure and radiation levels are acceptable."),
    ("Nuclear well-logging", "During nuclear well-logging, why must radioactive sources remain under continuous security control?", ["To prevent loss, theft and unauthorized exposure", "To improve battery life", "To reduce paperwork", "To increase source activity"], 0, "Continuous control prevents unauthorized access and supports prompt accountability."),
    ("Safe transport", "Which document is essential when transporting radioactive material?", ["Applicable transport documentation describing the consignment", "A personal shopping receipt", "An unrelated equipment manual", "A blank delivery note"], 0, "The consignment must be accompanied by the documentation required by applicable transport rules."),
    ("Package inspection", "What should be checked before dispatching a radioactive-material package?", ["Integrity, labels, markings, contamination and radiation levels", "Only the vehicle colour", "Only the recipient's telephone number", "Nothing if the package was previously used"], 0, "Pre-dispatch checks demonstrate that the package and its radiation controls are compliant."),
    ("Source accountability", "What is the main purpose of a radioactive-source inventory?", ["To maintain traceable control of each source and its location", "To replace personnel monitoring", "To calculate staff salaries", "To remove the need for leak tests"], 0, "An inventory provides traceability and supports rapid detection of a missing source."),
    ("Emergency response", "What is the first priority when a logging source is suspected to be lost downhole?", ["Protect people, control access and activate the approved emergency plan", "Send an untrained person to retrieve it", "Continue normal operations", "Conceal the incident"], 0, "Personnel protection and controlled implementation of the emergency plan take priority."),
    ("Dose control", "Which combination is fundamental for reducing external radiation dose?", ["Reduce time, increase distance and use suitable shielding", "Increase time, reduce distance and remove shielding", "Increase activity and reduce monitoring", "Remove barriers and warning signs"], 0, "Time, distance and shielding are the principal controls for external exposure."),
    ("Radiation surveys", "Why must a suitable radiation survey meter be function-checked before use?", ["To confirm it responds and is suitable for the intended measurement", "To replace calibration permanently", "To increase the source activity", "To avoid recording results"], 0, "A pre-use check helps detect an instrument that is not operating correctly."),
    ("Personnel monitoring", "Where should an occupational dosimeter normally be worn?", ["At the position specified by the radiation-protection programme", "Inside a vehicle glove box", "By another worker", "At home during the shift"], 0, "Correct wearing position is necessary for the result to represent the worker's exposure."),
    ("Controlled areas", "Why are barriers and warning signals used during industrial radiography?", ["To prevent unauthorized entry into an area where exposure may occur", "To advertise the company", "To replace source security", "To increase public access"], 0, "Barriers and warnings communicate the hazard and restrict access."),
    ("Transport index", "What does the transport index primarily help determine?", ["Radiation-control arrangements such as segregation during transport", "The financial value of the source", "The driver's licence expiry date", "The package colour preference"], 0, "The transport index is used in radiation control and segregation arrangements."),
    ("Contamination control", "What should be done when removable contamination above an applicable limit is detected on a package?", ["Control the package and follow approved decontamination and reporting procedures", "Dispatch it without action", "Wipe it with bare hands", "Remove all labels"], 0, "The package must be controlled and managed under approved contamination procedures."),
    ("Source storage", "A radioactive source that is not in use should be stored:", ["In an approved, secured and appropriately marked location", "In an unlocked public corridor", "Without an inventory entry", "Beside food supplies"], 0, "Secure approved storage maintains shielding, security and accountability."),
    ("Leak testing", "What is the purpose of a sealed-source leak test?", ["To detect possible loss of radioactive material from the source capsule", "To measure vehicle speed", "To replace source inventory", "To determine employee attendance"], 0, "Leak testing can identify failure of sealed-source containment."),
    ("Incident notification", "Why must a radiation incident be reported promptly through the approved chain?", ["To enable protection, assessment, corrective action and regulatory notification", "To prevent investigation", "To erase dose records", "To avoid emergency controls"], 0, "Prompt reporting enables an effective response and required notifications."),
    ("Training and competence", "Who should independently handle an industrial radiography or well-logging source?", ["A trained, competent and authorized person", "Any available visitor", "An untrained driver", "A person without local-rule knowledge"], 0, "Source handling requires appropriate training, competence and authorization."),
    ("Local rules", "What is the purpose of radiation-safety local rules?", ["To translate safety requirements into clear site-specific working instructions", "To replace all regulations", "To eliminate supervision", "To permit uncontrolled access"], 0, "Local rules state the practical controls workers must follow at the facility."),
    ("Vehicle safety", "Before transporting a radioactive consignment, the driver should verify that:", ["The load is secured and required documents, labels and emergency information are present", "The labels have been removed", "The package can move freely", "No emergency contact is available"], 0, "Load security and required communication materials are essential transport controls."),
    ("Optimization", "Which action best demonstrates optimization of protection during a source operation?", ["Planning the task to minimize time near the source while using distance and shielding", "Repeating the exposure unnecessarily", "Disabling area monitors", "Allowing unplanned access"], 0, "Planning and engineering controls reduce dose while achieving the task."),
    ("Records", "Which record best supports regulatory traceability for a source movement?", ["A dated record identifying the source, sender, recipient and responsible persons", "An unsigned blank form", "An informal memory of the event", "A document with no source identifier"], 0, "Complete dated records provide an auditable chain of custody."),
]


async def main():
    async with AsyncSessionLocal() as db:
        program = (await db.execute(select(TrainingProgram).where(func.upper(TrainingProgram.code) == "NWLST"))).scalar_one_or_none()
        if not program:
            raise SystemExit("NWLST training programme was not found")
        bank = (await db.execute(select(QuestionBank).where(
            QuestionBank.training_program_id == program.id, QuestionBank.name == BANK_NAME
        ))).scalar_one_or_none()
        if bank is None:
            bank = QuestionBank(training_program_id=program.id, name=BANK_NAME,
                                description=f"One hundred editable questions for {program.name}. Review and approve before examination use.",
                                is_active=True, created_at=datetime.utcnow())
            db.add(bank)
            await db.flush()
        existing = (await db.execute(select(func.count(Question.id)).where(Question.question_bank_id == bank.id, Question.is_active == True))).scalar_one()
        targets = balanced_correct_labels(max(0, 100-existing))
        for index in range(existing, 100):
            topic, stem, choices, correct, explanation = BASES[index % len(BASES)]
            scenario = index // len(BASES) + 1
            options, answer = redistribute_options({chr(65+i):value for i,value in enumerate(choices)},
                                                    [chr(65+correct)], targets[index-existing])
            db.add(Question(question_bank_id=bank.id, question_text=f"NWLST scenario {scenario}: {stem}",
                            question_type=QuestionType.MULTIPLE_CHOICE,
                            options=options,
                            correct_answer=answer, explanation=explanation,
                            marks=1.0, difficulty="easy" if index % 4 == 0 else "medium",
                            topic=topic, is_active=True, created_at=datetime.utcnow()))
        await db.commit()
        final_count = (await db.execute(select(func.count(Question.id)).where(Question.question_bank_id == bank.id, Question.is_active == True))).scalar_one()
        print(f"NWLST bank {bank.id}: {final_count} questions")


if __name__ == "__main__":
    asyncio.run(main())
