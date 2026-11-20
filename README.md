## Program: Syria Tech Harvest — Season 3

This project is being developed as part of **Syria Tech Harvest — Season 3**, a free technical training program and national platform for turning local challenges into innovative technical solutions. The program brings together creative youth with owners of real-world challenges to produce prototypes of practical, applicable solutions with sustainable impact.

**Program details:**

- **Activity type:** Challenge-Based Competition
- **Supported and funded by:** United Nations Population Fund (UNFPA Syria)
- **Camp start date:** Tuesday, 27/10/2026
- **Program duration:** Six weeks
- **Team size:** 2 to 3 members
- **Timing:** Intermittent evening sessions
- **Training location:** Damascus — [Digit Center](https://digit-sy.com/en), Kafr Sousa, Carlton Roundabout, after the National Hospital
- **Organizer:** [Digit](https://digit-sy.com/en) — Powered by [Sanad Youth for Development](https://sanadyouth.org)

**Incubation opportunity:** Outstanding teams receive an incubation opportunity inside the [digital fabrication lab (Fab Lab)](https://www.fablabs.io/) at Digit Center, including training on lab equipment, prototype development, technical and entrepreneurial mentoring, technical support for product improvement, and potential connections with partners or investors.

**Competition phases:** Direct problem analysis with problem owners → Research and development → Designing and building a digital prototype → Developing and testing the solution → Presentation before the judging committee.

**Evaluation criteria:** Understanding of the problem, quality of the solution, applicability, and balance of roles within the team.

**Selected community challenges:** This project addresses the following challenges from the Damascus Governorate complaint list: encroachments harming public rights (5,494 complaints), old abandoned vehicles requiring removal (2,257 complaints), unlicensed parking space occupation (1,941 complaints), and accumulated waste (2,383 complaints).

---

## Technical Architecture (Brief)

- **Edge nodes:** ESP32 + ESP32-CAM + ultrasonic sensor, solar + battery, local change detection to minimize transmissions.
- **Network:** ESP-NOW — direct device-to-device communication, no router or telecom network required.
- **Server:** Microservices (Docker + FastAPI) + YOLO inference + alert rules + permission management and API generation.
- **Dashboard:** Centralized alerts and images, threshold configuration, priority scoring, and remediation tracking.
- **Privacy:** Automatic redaction of faces, license plates, and personal information before display.

---

## Team

| Name | Title | Responsibility | Background | Contact |
|------|-------|----------------|------------|---------|
| Ali Shawqal | **CMO** (Chief Marketing Officer) | Business logic,  public face: user interface, outreach, and presentation | B.Sc. Civil Engineer; M.Sc. Urban Planning (Urban Engineering & Sustainable Urban Development); MBA | ali2damas@gmail.com · 0932047725 · [LinkedIn](https://www.linkedin.com/in/ali-shawqal) |
| Ahmad Deeb | **CTO** (Chief Technology Officer) | Technical software: AI/Expert Systems, networking, platform architecture | Civil Engineer; AI Engineer; Master in Networking | ahmaddeebdev@gmail.com · [LinkedIn](https://www.linkedin.com/in/ahmad-deeb-13699b39a/) |
| Yousef Alkurabi | **CDO** (Chief Design Officer) | 3D modeling and printing, edge hardware enclosure and physical design | Civil Engineer | Korabika1998@outlook.com · 0958210581 · [LinkedIn](https://www.linkedin.com/in/yousef-alkurabi-93779819b) |
| Diaa Al Din Aldebes | **CPO** (Chief Product Officer) | Work interface, operations, and decision-support integration | Civil Engineer | diaa.aldebes@gmail.com · 0931493195 · [LinkedIn](https://www.linkedin.com/in/diaa-al-din-aldebes-000b2a295) |

---

## Program Deliverables

**Status legend:** ✅ Completed · 🕐 In progress · ⬜ Not started

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **Phase 1: Ideation & Research** | Problem definition and justification | 🕐 |
| | Stakeholder Map | 🕐 |
| | User Personas | 🕐 |
| | Initial Feasibility Assessment | 🕐 |
| | Value Proposition Canvas | 🕐 |
| **Phase 2: Prototyping & MVP** | Prototype plan and specifications | ⬜ |
| | Technical designs and schematics | ⬜ |
| | Testable working prototype | ⬜ |
| | Test results and technical challenges log | ⬜ |
| | Improved prototype version | ⬜ |
| **Phase 3: Presentation & Demo** | Professional Pitch Deck | ⬜ |
| | Live Demo preparation | ⬜ |

---

## Timeline & Sprint Progress

**Status legend:** ⬜ Not started · 🕐 In progress · 🔍 Review · ✅ Done

The project starts **November 1, 2026**, organized into **6 one-week sprints**.

| Epic | Sprint | Dates | Status |
|------|--------|-------|--------|
| Epic 1: Edge Node Hardware & Firmware | Sprint 1 | Nov 1 – Nov 7, 2026 | 🕐 |
| Epic 2: ESP-NOW Network Layer | Sprint 1 | Nov 1 – Nov 7, 2026 | ⬜ |
| Epic 8: Repository README & Docs | Sprint 1 | Nov 1 – Nov 7, 2026 | ⬜ |
| Epic 3: Backend Platform | Sprint 2 | Nov 8 – Nov 14, 2026 | ⬜ |
| Epic 4: Detection Modules (Waste, Encroachment) | Sprint 2 | Nov 8 – Nov 14, 2026 | ⬜ |
| Epic 6: Dashboard (alerts list, detail) | Sprint 3 | Nov 15 – Nov 21, 2026 | ⬜ |
| Epic 5: Privacy Layer | Sprint 4 | Nov 22 – Nov 28, 2026 | ⬜ |
| Epic 6: Dashboard (map, thresholds, priority view) | Sprint 4 | Nov 22 – Nov 28, 2026 | ⬜ |
| Epic 7: Deployment & Pilot Scope | Sprint 5 | Nov 29 – Dec 5, 2026 | ⬜ |
| Epic 8: Proposal & Demo Video | Sprint 6 | Dec 6 – Dec 12, 2026 | ⬜ |

---

## Project Board

Track progress on the [DamasucEye Kanban board](https://github.com/users/AhmedGDeeb/projects/5/views/2).

---

## Project Status

🚧 Early development stage — prototype under construction.

---

## License

This project is licensed under the **Creative Commons Attribution (CC BY)** license.

You are free to share and adapt the material for any purpose, even commercially, as long as appropriate credit is given to the DamascusEye team.