typedef struct Node {
    int value;
} Node;

typedef union Value {
    int i;
    float f;
} Value;

typedef struct Inner {
    int value;
} Inner;

typedef struct Outer {
    Inner inner;
    int values[4];
} Outer;

extern unsigned int g_count;
extern unsigned int *g_ptr;
extern char s_line[16];
extern unsigned int s_line_len;
extern Node *g_node;
extern unsigned int g_array[4];
extern Node *g_ptr_array[2];
extern Outer g_struct;

void pointer_ops(Node *node, unsigned int *p, unsigned int *array);
void *allocate_value(void);
void conversion_ops(Node *p);
void arithmetic_ops(int *p, int *q, int n);
void global_ops(unsigned int value, unsigned int index);
