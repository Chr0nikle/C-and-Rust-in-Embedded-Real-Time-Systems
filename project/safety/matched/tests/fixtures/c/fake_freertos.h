typedef void *QueueHandle_t;
typedef int BaseType_t;
typedef unsigned TickType_t;
typedef unsigned size_t;
typedef void (*TaskFunction_t)(void *);
#define NULL 0

#define pdPASS 1

QueueHandle_t xQueueGenericCreate(unsigned length, unsigned item_size, unsigned char type);
BaseType_t xQueueGenericSend(QueueHandle_t queue, const void *item, TickType_t ticks, BaseType_t position);
BaseType_t xQueueReceive(QueueHandle_t queue, void *out, TickType_t ticks);
BaseType_t xQueueGenericSendFromISR(QueueHandle_t queue, const void *item, BaseType_t *woken);
void vQueueDelete(QueueHandle_t queue);
void *pvPortMalloc(unsigned size);
void *pvPortMallocX(size_t xWantedSize);
void vPortFree(void *p);
unsigned xPortGetFreeHeapSize(void);
BaseType_t xTaskCreate(TaskFunction_t pxTaskCode, const char *pcName, unsigned uxStackDepth, void *pvParameters, unsigned uxPriority, QueueHandle_t *pxCreatedTask);

#define xQueueSend(queue, item, ticks) xQueueGenericSend((queue), (item), (ticks), 0)
#define xQueueCreate(length, size) xQueueGenericCreate((length), (size), 0)
