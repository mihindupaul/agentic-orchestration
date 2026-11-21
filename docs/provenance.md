# Provenance

Where this project comes from, what it is built on, and what it is not.

## Why this project exists
I am an engineer who loves automation and wants to learn how to build agentic orchestration systems: software in
which an AI agent builds, checks and runs workflows that drive machines. To do that I needed something to
orchestrate. I chose laboratory automation, a domain I only recently got to know, as the worked example, and
built a simulated workcell to practise on. The domain is the idea; the design is mine.

I no longer have access to lab hardware, which is why everything runs against a simulator.

## What it is built from
- **Public documentation and standards:** DOBOT's public TCP/IP command guide for the arm's command style, the SBS
  microplate footprint for rack dimensions, and the published sources and documentation of Orca and cheshire-drivers
  for the driver plug-in.
- **General engineering knowledge:** SCARA kinematics, TCP text protocols, simulation, async Python, ROS2 and RViz.
- **My own design:** the imaginary workcell, its layout, geometry, timings, device protocols, fault behaviour,
  deck file, controller and operations. These are invented for this project and listed in
  [`assumptions.md`](assumptions.md).

## What it is not built from
I did not use any confidential or proprietary material from any employer or other party: no source code,
documents, drawings, specifications, protocols, parts lists, customer information, screenshots or footage. The
workcell does not represent any real instrument.

## Timeline
- Started: 20/11/2026
- First public commit: see the repository history
- Tools: built with AI assistance (Claude), reviewed and tested by me

## Names
DOBOT, Orca, PyLabRobot, ROS2 and other product names are used only to describe compatibility. This is an
independent educational project, not affiliated with or endorsed by their owners.
