// --- Standalone preview bootstrap ---------------------------------------------
// When this chapter is compiled on its own (CLI or editor preview), the block
// below applies the thesis template and bibliography so the chapter renders in
// context. When thesis.typ includes this file it first sets the
// "_chapter_embedded" state, which disables the bootstrap because the thesis
// already supplies the template and bibliography. This is preview scaffolding,
// not part of the assembled thesis layout.
#import "@preview/uastw-thesis:0.1.0" as uastw
#show: it => context {
 if state("_chapter_embedded", false).get() { it }
 else { uastw.uastw-thesis-page-setup(it) }
}
#show "LaTeX": uastw.latex
#show "BibTeX": uastw.bibtex
// ---[ end standalone preview bootstrap ]---------------------------------------

#heading(outlined: false, bookmarked: true, numbering: none)[Kurzfassung]

#set par(spacing: 0.9em)

Diese Masterarbeit vergleicht die Programmiersprachen C und Rust beim Einsatz auf eingebetteten Echtzeitbetriebssystemen. Untersucht werden Ausführungszeit, Speicherbedarf, Speichersicherheit und Implementierungskomplexität. Der Vergleich umfasst vier Hardwareplattformen sowie die Betriebssysteme FreeRTOS, NuttX und Zephyr, auf denen dieselben Testfälle in C und Rust umgesetzt wurden. Zusätzlich werden die Rust-Kernel FreeRTOS-in-Rust und Rivet betrachtet.

Für den Vergleich wurde ein gemeinsames Testverfahren mit 26 Testfällen entwickelt, die Task-Verwaltung, Synchronisation, Task-Kommunikation und Interruptverarbeitung untersuchen. Insgesamt wurden 27 Kombinationen aus Hardware, Betriebssystem und Programmiersprache auf den jeweiligen Geräten in zehn unabhängigen Messläufen getestet. Neben den Laufzeiten wurden auch der Speicherbedarf, sicherheitsrelevante Operationen im Quellcode und die Komplexität der Implementierungen untersucht.

Die Messungen zeigen, dass C und Rust auf derselben Hardware und mit demselben Betriebssystem in den meisten Testfällen ähnlich schnell sind. Deutlichere Unterschiede treten vor allem bei der Kommunikation zwischen Tasks auf. Auch beim Speicherbedarf gibt es Unterschiede: Während der zusätzliche Arbeitsspeicherbedarf pro Task identisch ist, benötigen die Rust-Implementierungen meist mehr Flash-Speicher und teilweise auch mehr statischen Arbeitsspeicher.

Im untersuchten Rust-Anwendungscode wurde keine direkte Rohzeigeroperation eindeutig aufgelöst; je NuttX-Konfiguration blieb ein Eintrag ungeklärt. Im entsprechenden C-Code kommen solche Operationen dagegen vor. Sobald Rust mit einem in C geschriebenen Betriebssystemkern zusammenarbeitet, sind jedoch weiterhin Operationen notwendig, deren Sicherheit der Rust-Compiler nicht überprüfen kann. Die Ergebnisse zeigen somit, wo solche Operationen auftreten, erlauben aber keine Aussage darüber, ob ein vollständiges System speichersicher ist. Darüber hinaus benötigen die Rust-Implementierungen weniger selbst geschriebenen Code, erfordern aber zusätzliche Schnittstellen zum Betriebssystem.

Rust erreicht in den untersuchten Konfigurationen eine mit C vergleichbare Laufzeitleistung; bei der Sprachwahl sind Speicherbedarf, Schnittstellen und Wartungsaufwand zu berücksichtigen. Für die übernommenen Ergebnisse der Rust-basierten Kernel fehlen die Rohdaten.

*Schlagwörter:* Eingebettete Systeme, Echtzeitbetriebssysteme, Programmiersprachenvergleich, Rust, Speicherbedarf, Speichersicherheit, Laufzeitverhalten, Reproduzierbarkeit
