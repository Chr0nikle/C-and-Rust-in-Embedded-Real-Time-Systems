// -----------------------------------------------------------------------------
// THESIS TEMPLATE FOR UAS TECHNIKUM WIEN
// Author: M. Horauer
// GITHUB: https://github.com/mhorauer/
// LICENSE: GPL-3.0-or-later
// -----------------------------------------------------------------------------

#import "@preview/uastw-thesis:0.1.0": *
#import "@preview/note-me:0.6.0": note, important, warning, tip

// Marks the document as assembled: chapter files use this to skip their
// standalone-preview bootstrap. See sections/methodology.typ.
#state("_chapter_embedded").update(true)

// -----------------------------------------------------------------------------
// ---[ ToDo ]------------------------------------------------------------------
// Adjust the variables below ...

//#let lan = "de"
#let lan = "en"
//#let deg = "Bachelor"
#let deg = "Master"
#let std = "Embedded Systems"
//#let std = "Information- and Communication Systems"

#let title = "C and Rust in Embedded Real-Time Systems"
#let subTitle = "A Controlled Study of Performance, Memory Usage, and Safety Boundaries"

#let authorName = "Simon Nenning BSc."
#let authorID = "2410297031"

#let adv1 = "FH-Prof. Dipl.-Ing. Dr. Martin Horauer"
#let adv2 = "Volker Tenta, MSc."
#let loc = "Wien"

// -----------------------------------------------------------------------------
// ---[ DO NOT TOUCH ]----------------------------------------------------------
#let thesisType = ""
#{
  if lan == "de" {
	if deg == "Bachelor" {
		thesisType = "BACHELORARBEIT"
	} else {
		thesisType = "MASTERARBEIT"
	}
 } else {
	if deg == "Bachelor" {
		thesisType = "BACHELOR PAPER"
	} else {
		thesisType = "MASTER THESIS"
	}
 }
}
// --- OUTPUT THE TITLEPAGE ----------------------------------------------------
#set page(numbering: none)
#show: uastw-thesis-titlepage.with(
	language: lan, 
    thesis-type: thesisType, 
    degree: deg, 
    study-program: std, 
    thesis-title: title, 
    thesis-subtitle: subTitle, 
    author: authorName, 
    authorid: authorID, 
    advisor1: adv1,
    advisor2: adv2,    
    location: loc)

// --- SETUP THE PAGE STYLING & SOME VARIABLES ---------------------------------
#show: uastw-thesis-page-setup
#show colbreak: pagebreak(weak: true)
#show "LaTeX": latex 
#show "BibTeX": bibtex 
// Keep language names as plain text in thesis prose and bibliographic titles.

// --- IEEE caption style (Fig. N, Table I, Listing N) -------------------------
#show figure.where(kind: image): set figure(numbering: "1", supplement: [Fig.])
#show figure.where(kind: table): set figure(numbering: "I", supplement: [Table])

// --- OUTPUT THE PAGE OF DECLARATION ------------------------------------------
#show: declaration.with(
	language: lan)

// --- WE START WITH PAGE NUMBERING @KURZFASSUNG -------------------------------
#set page(footer: context [
	#set text(twgray, size: 10pt) 
	#align(right, counter(page).display("1"))
	])
#set page(numbering: "1")

// --- INSERT KURZFASSUNG ------------------------------------------------------
#include "sections/kurzfassung.typ"

// --- INSERT ABSTRACT ---------------------------------------------------------
#include "sections/abstract.typ"

// --- INSERT ACHNOWLEDGMENT ---------------------------------------------------
#include "sections/acknowledgement.typ"

// --- INSERT TABLE OF CONTENTS ------------------------------------------------
#outline(
	title: if lan == "en" [Table of Contents] else [Inhaltsverzeichnis]
)

// Just in case - reset the counter for Headings ...
//
#counter(heading).update(0)
// =============================================================================
// ---[ MAIN CHAPTERS ]----------------------------------------------------------
// Write chapter content in sections/. This file configures and assembles it.
#include "sections/introduction.typ"
#include "sections/background.typ"
#include "sections/state-of-the-art.typ"
#include "sections/experimental-setup.typ"
#include "sections/methodology.typ"
#include "sections/results.typ"
#include "sections/discussion.typ"
#include "sections/conclusion.typ"

// -----------------------------------------------------------------------------
// ---[ BIBLIOGRAPHY ]----------------------------------------------------------
#bibliography(
	title: if lan == "en" [Bibliography] else [Literaturverzeichnis],
	style: "ieee",
	"references/works.bib"
)
// -----------------------------------------------------------------------------
// ---[ OPTIONAL BACK MATTER ]--------------------------------------------------
// Uncomment these includes once their placeholder content has been replaced.
// #include "sections/lists.typ"
#include "sections/abbreviations.typ"
#include "sections/aitools.typ"

// ---[ APPENDIX ]---------------------------------------------------------------
#counter(heading).update(0)
#import "styles/appendix-tables.typ": appendix-tables
#show: appendix-tables
#set heading(numbering: "A.1.1", supplement: [Appendix])
#include "sections/benchmark-specification.typ"
#include "sections/benchmark-results.typ"
#include "sections/memory-results.typ"
#include "sections/safety-results.typ"
#include "sections/integration-results.typ"

// #include "sections/appendix-a.typ"
// #include "sections/index.typ"

// EOF
