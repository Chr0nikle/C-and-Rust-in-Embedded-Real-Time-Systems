
// --- Standalone preview bootstrap ---------------------------------------------

#import "@preview/uastw-thesis:0.1.0" as uastw
#import "../styles/callouts.typ": takeaway

#show: it => context {
  if state("_chapter_embedded", false).get() { it }
  else { uastw.uastw-thesis-page-setup(it) }
}

#show "LaTeX": uastw.latex
#show "BibTeX": uastw.bibtex

// --- End standalone preview bootstrap -----------------------------------------


= Background

Comparing C and Rust on embedded real-time operating systems (RTOS) requires an understanding of how these systems operate and how their performance is measured. Important concepts include task scheduling, execution time, response time, and deadlines.

This chapter introduces these concepts and explains the role of memory management, programming language characteristics, and C/Rust interoperability in embedded systems. It provides the technical foundation for the experimental methodology in the Methodology chapter.


== Task Timing Concepts

In real-time systems, correct behaviour depends not only on producing the expected result but also on producing it within a required time frame. A task may perform its computation correctly and still fail if it finishes too late. Understanding execution time, response time, and deadlines is therefore essential when evaluating RTOS performance.

#figure(
  image("../figures/task-timing.svg", width: 100%),
  caption: [Task timing relationships],
) <task-timing>

@task-timing illustrates a task's execution from release to deadline. After release, the task initially waits for the processor, shown in grey. It then executes, shown in blue, before being preempted, shown in orange. Once it resumes, it continues executing until completion.

These intervals are used to distinguish two timing measurements @Liu2000:

- *Execution time:* The total time a task spends running on the processor, shown by the two blue intervals.
- *Response time:* The time from task release to completion, including waiting and preemption.

The difference matters because a task may require little processor time but still finish too late due to scheduling delays. In @task-timing, the task completes before its deadline. The remaining time is called the *deadline margin*. Additional delays would reduce this margin and could eventually cause a deadline miss.

However, the diagram represents only one execution. Measured response times do not establish a worst-case bound or guarantee that future deadlines will be met.

#takeaway[
  Execution time measures processor usage, while response time includes waiting and preemption. Distinguishing them helps interpret RTOS timing measurements, but observed times do not guarantee future deadlines.
]


== Task Scheduling and Priorities

Task response time includes periods when a task is waiting rather than executing. These delays depend partly on how the RTOS schedules tasks and manages their priorities.

In *preemptive scheduling*, a higher-priority task can interrupt a running lower-priority task when it becomes ready. In *cooperative scheduling*, the running task continues until it yields or blocks, even if a higher-priority task is ready to execute @Buttazzo2024.

#figure(
  image("../figures/scheduling-gantt.svg", width: 100%),
  caption: [Preemptive and cooperative scheduling],
) <scheduling-modes>

@scheduling-modes compares both scheduling modes using the same task scenario. The dashed line marks when the high-priority task becomes ready. Under cooperative scheduling, its execution is delayed until the running task yields or blocks.

Scheduling also affects tasks that wait for a specified amount of time. How precisely the kernel handles these delays is called *scheduling granularity*.

One way an RTOS manages these delays is through a periodic *system tick*. This is a timer interrupt that occurs at fixed intervals. The kernel uses each tick to update its timing information and check whether tasks should wake up. For example, with a system tick of 1 ms, a requested delay of 0.5 ms may be rounded up to a full tick. The task then waits longer than requested because of insufficient scheduling granularity. This increases its response time without increasing the time it spends executing on the processor.

While scheduling granularity affects when a task can resume, *measurement resolution* determines how precisely its timing can be measured. It describes the smallest time interval a timer can distinguish @Buttazzo2024.

For example, a hardware counter may measure time in microseconds even when the kernel schedules delays in milliseconds. This allows both execution time and response time to be measured more precisely, but does not reduce the task's waiting time. Sufficient measurement resolution is therefore important for distinguishing actual execution costs from delays introduced by the RTOS.

Waiting times can also arise when tasks compete for shared resources. A *mutex* protects a shared resource by allowing only one task to hold it at a time. If another task attempts to acquire the same mutex using a blocking operation, it must wait until the current holder releases it.

This can lead to *priority inversion*. For example, a low-priority task may acquire a mutex before a high-priority task needs it. The high-priority task then blocks until the mutex is released. However, if a medium-priority task becomes ready, it can preempt the low-priority task. This prevents the mutex holder from continuing its work and extends the waiting time of the high-priority task.

*Priority inheritance* addresses this problem by temporarily raising the mutex holder's priority to that of the waiting high-priority task. This prevents medium-priority tasks from preempting the mutex holder while it completes its critical section. Once the mutex is released, the holder's priority is restored, and the high-priority task can proceed @Sha1990.

#figure(
  image("../figures/priority-inheritance-timelines.svg", width: 100%),
  caption: [Priority inversion and inheritance],
) <priority-inheritance>

@priority-inheritance illustrates how priority inheritance changes task execution and waiting time.

#takeaway[
  Scheduling policies, timer granularity, and resource contention influence how long tasks wait. These effects can change benchmark timings independently of whether the application is written in C or Rust.
]


== Event Handling and Task Communication

Task execution is influenced not only by scheduling decisions, but also by events that occur outside the running task. Such events may require immediate attention or cause a waiting task to become ready.

Interrupts allow an RTOS to respond to external events without requiring a task to continuously check for them. When an interrupt occurs, the processor temporarily suspends its current execution and runs an *interrupt service routine* (ISR) @Liu2000.

The ISR handles time-sensitive work, while more complex processing can be deferred to a regular task. For example, an ISR may signal that new sensor data is available. A task waiting for this event then becomes ready to execute. However, it may still need to wait until the scheduler allows it to run.

Interrupt handling therefore involves two distinct delays. The processor must first respond to the interrupt, and the waiting task must then be scheduled to run.

#figure(
  image("../figures/interrupt-flow.svg", width: 100%),
  caption: [Interrupt handling sequence],
) <interrupt-flow>

@interrupt-flow illustrates these stages and distinguishes two timing measurements:

- *Interrupt latency:* The time from the interrupt trigger to ISR entry. It includes delays caused by interrupt masking, higher-priority interrupts, and the processor's initial interrupt handling.
- *ISR-to-task latency:* The time from ISR entry until the waiting task resumes execution. It includes ISR execution, task notification, and any scheduling delay before the task runs.

Interrupt handling can also be affected by other system activity. For example, interrupts may be temporarily disabled during critical sections, delaying the execution of an ISR @Buttazzo2024. Higher-priority interrupts may also interrupt an ISR that is already running. This is known as *nested interrupt handling* @Liu2000. Both situations can introduce additional delays and affect the measured interrupt response time.

Beyond handling the interrupt itself, the RTOS must often coordinate further processing between tasks. This involves *synchronisation* and communication mechanisms. Message queues transfer information between tasks, semaphores can signal events, and mutexes protect shared resources.

These operations may also introduce delays. For example, sending a message through a queue may require copying its contents @FreeRTOSQueues. If the queue is full, the sending task may have to wait until space becomes available @Buttazzo2024. The resulting latency therefore depends on factors such as payload size and blocking behaviour.

#takeaway[
  Event handling and task communication introduce delays at different stages. Interrupt handling, scheduling, payload size, and blocking behaviour must be considered when interpreting RTOS latency measurements.
]


== Memory and Resource Accounting

Besides timing behaviour, memory consumption is an important consideration in embedded systems, where available flash and RAM are often limited. Memory requirements can be divided into *static footprint* and *runtime memory usage*.

The static footprint is determined from the compiled program. Code and constant data generally occupy flash, while initialised and zero-initialised data require RAM. Initial values for initialised data are typically stored in flash and copied to RAM during startup. The memory required for code and data can be examined using the sections of the Executable and Linkable Format (ELF) file.

Runtime memory use, by contrast, depends on program execution and includes stack consumption and heap allocations. Measuring peak stack and heap usage helps estimate the memory required under a particular workload.

Memory management can also introduce execution-time overhead. Each task requires kernel bookkeeping structures, while dynamic allocation takes time and may lead to heap fragmentation. Fragmentation occurs when free memory is divided into separate blocks, potentially preventing larger allocations even when sufficient total memory remains @Wilson1995.

#takeaway[
  Static footprint, runtime memory use, and allocation behaviour describe different resource costs. Comparing them alongside timing measurements gives a more complete picture of C and Rust implementations.
]


== Memory Safety and Interoperability

Besides the amount of memory an application requires, it is also important how that memory is managed and accessed. This is a key difference between C and Rust.

In C, programmers must ensure that pointers remain valid, memory is managed correctly, and shared resources are accessed safely. Rust uses *ownership* and *borrowing* to enforce many of these rules at compile time. Each value has an owner, while the borrow checker restricts how references can be used and how long they remain valid @RustOwnership.

These differences become particularly important when applications interact with an RTOS. Resources such as task handles, queues, and mutexes are managed by the kernel. Applications must therefore follow rules governing their creation, use, and destruction. Rust bindings can express some of these rules through types and lifetimes, while C generally relies on API contracts and programmer discipline.

However, Rust cannot automatically verify the behaviour of external C code. Operations such as dereferencing raw pointers and calling unsafe functions require explicit *unsafe* contexts. Within these contexts, the programmer must uphold safety conditions that the compiler cannot verify @RustUnsafe.

This is particularly relevant to the *foreign-function interface* (FFI), which allows Rust to interact with C code. Both languages must agree on calling conventions, data representation, and resource ownership. Rust's `extern "C"` declarations specify the calling convention but do not guarantee that the underlying C functions behave correctly @RustReference.

Rust bindings may hide these unsafe operations behind a safe API. Such an interface allows application code to interact with the kernel without directly using `unsafe`. However, its safety depends on whether the binding implementation correctly maintains the required conditions.

#figure(
  image("../figures/c-rust-kernel-interfaces.svg", width: 100%),
  caption: [C and Rust kernel interfaces],
) <c-rust-kernel-interfaces>

@c-rust-kernel-interfaces illustrates how C and Rust applications can interact with a shared C-based RTOS. While C accesses the kernel through its API, Rust may use a safe binding interface built on unsafe FFI calls. Both approaches depend on kernel and external C code that Rust's compiler cannot verify.

Language bindings may introduce additional function calls, data conversions, or resource management that influence measured performance.

#takeaway[
  Rust's compile-time safety guarantees do not automatically extend through FFI to a C-based RTOS. Both the safety boundaries and potential performance costs of language bindings must be considered when comparing C and Rust.
]

== Software Complexity and Maintainability

Besides performance, memory usage and safety, the effort required to understand and maintain software is important in embedded systems. Source-code metrics provide indicators of this effort, although they cannot measure maintainability directly @ISO25010.

Source lines of code describe the amount of code that needs to be implemented and maintained. Cyclomatic complexity measures independent paths through a program's control flow and is commonly used to assess testing complexity @McCabe1976. Cognitive complexity considers how difficult the control flow is to understand @Campbell2018. Module coupling describes dependencies between software components, which can increase the effort needed to make changes @Briand1999.

Together, these metrics provide different perspectives on implementation complexity. They are particularly relevant when comparing C and Rust applications that perform the same tasks but require different adapters, interfaces and supporting code. Lower metric values do not necessarily mean that software is easier to maintain, but they help identify differences that warrant closer examination.



#context if not state("_chapter_embedded", false).get() {
  bibliography("../references/works.bib")
}
