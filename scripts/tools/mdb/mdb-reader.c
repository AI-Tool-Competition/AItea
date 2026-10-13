/* SPDX-License-Identifier: LGPL-2.0-or-later
 * Read-only MDB storage exporter for AItea. No SQL, macros or shell execution.
 * Uses the public libmdb 1.0.0 API (LGPL-2.0-or-later), not mdb-export code.
 * Memo format adaptation based on MDB Tools data.c, Copyright (C) 2000 Brian Bruns.
 * Format references and package/build provenance are documented in README.md.
 */
#define _GNU_SOURCE
#include <mdbtools.h>
#include <sched.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <signal.h>
#include <errno.h>
#include <stdint.h>
#include <dlfcn.h>

#define PROTOCOL "aitea-mdb-storage-v1"
#define MAX_FILE (20 * 1024 * 1024)
#define MAX_VALUE (2 * 1024 * 1024)
#define MAX_ROWS 20000
#define MAX_TABLES 64
#define MAX_COLUMNS 128
#define MAX_CHUNKS 4096

static void fail(const char *reason) {
    fprintf(stderr, "mdb-reader: %s\n", reason);
    exit(2);
}

static void quoted(const char *value) {
    if (!value || !g_utf8_validate(value, -1, NULL)) fail("invalid UTF-8 metadata");
    putchar('"');
    for (const unsigned char *p = (const unsigned char *)value; *p; p++) {
        if (*p == '"' || *p == '\\') { putchar('\\'); putchar(*p); }
        else if (*p < 32) printf("\\u%04x", *p);
        else putchar(*p);
    }
    putchar('"');
}

static void digest(const void *data, size_t length) {
    char *hash = g_compute_checksum_for_data(G_CHECKSUM_SHA256, data, length);
    quoted(hash);
    g_free(hash);
}

static void encoded(const void *data, size_t length) {
    if (length > MAX_VALUE) fail("field exceeds byte limit");
    char *value = g_base64_encode(data, length);
    quoted(value);
    g_free(value);
}

static void write_proc(const char *name, const char *data) {
    char path[80];
    snprintf(path, sizeof(path), "/proc/self/%s", name);
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    if (fd < 0 || write(fd, data, strlen(data)) != (ssize_t)strlen(data)) fail("network isolation unavailable");
    if (close(fd)) fail("cannot close namespace map");
}

static void isolate(pid_t expected_parent) {
    if (getppid() != expected_parent || expected_parent <= 1) fail("parent identity changed");
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != expected_parent) fail("parent death protection unavailable");
    uid_t uid = getuid(); gid_t gid = getgid(); char map[80];
    if (unshare(CLONE_NEWUSER)) fail("user namespace unavailable");
    write_proc("setgroups", "deny");
    snprintf(map, sizeof(map), "0 %lu 1\n", (unsigned long)uid); write_proc("uid_map", map);
    snprintf(map, sizeof(map), "0 %lu 1\n", (unsigned long)gid); write_proc("gid_map", map);
    if (unshare(CLONE_NEWNET)) fail("network namespace unavailable");
    /* UID/GID changes reset PDEATHSIG on Linux. Reinstate it before parsing. */
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) || getppid() != expected_parent || prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
        fail("process protection unavailable");
    struct rlimit cpu = {20, 20}, mem = {512 * 1024 * 1024, 512 * 1024 * 1024}, core = {0, 0};
    if (setrlimit(RLIMIT_CPU, &cpu) || setrlimit(RLIMIT_AS, &mem) || setrlimit(RLIMIT_CORE, &core))
        fail("resource limits unavailable");
    alarm(25);
}

static void storage(unsigned int page, unsigned int slot, int start, size_t length, const void *data) {
    printf("{\"database_page\":%u,\"row_slot\":%u,\"start\":%d,\"length\":%zu,\"sha256\":", page, slot, start, length);
    digest(data, length); putchar('}');
}

/* Reconstruct raw Memo bytes, retaining every actual MDB storage locator.
 * The Jet flags/chaining format is described by upstream libmdb/data.c v1.0.0.
 * No string conversion with libmdb's fixed/truncating export buffer is used.
 */
static void memo(MdbHandle *mdb, MdbField *field) {
    if (field->siz < MDB_MEMO_OVERHEAD) fail("incomplete memo header");
    unsigned char *raw = mdb->pg_buf + field->start;
    uint32_t flags = mdb_get_int32(raw, 0), expected = flags & 0x00ffffff;
    uint32_t pointer = mdb_get_int32(raw, 4);
    if (expected > MAX_VALUE) fail("memo exceeds byte limit");
    GByteArray *payload = g_byte_array_new();
    printf("{\"storage\":[");
    if (flags & 0x80000000u) {
        size_t length = field->siz - MDB_MEMO_OVERHEAD;
        if (length != expected) fail("inline memo length mismatch");
        g_byte_array_append(payload, raw + MDB_MEMO_OVERHEAD, length);
    } else {
        int single = !!(flags & 0x40000000u);
        if (!single && (flags & 0xff000000u)) fail("unsupported memo flags");
        if (!pointer && expected) fail("missing memo chain");
        GHashTable *visited = g_hash_table_new(g_direct_hash, g_direct_equal);
        unsigned int count = 0;
        while (pointer) {
            if (++count > MAX_CHUNKS || g_hash_table_contains(visited, GUINT_TO_POINTER(pointer))) fail("memo cycle or chunk limit");
            g_hash_table_add(visited, GUINT_TO_POINTER(pointer));
            void *buffer; int start; size_t length;
            if (mdb_find_pg_row(mdb, pointer, &buffer, &start, &length) || length > 4096) fail("memo storage unavailable");
            if (count > 1) putchar(',');
            storage(pointer >> 8, pointer & 0xff, start, length, (unsigned char *)buffer + start);
            size_t prefix = single ? 0 : 4;
            if (length < prefix || payload->len + length - prefix > MAX_VALUE) fail("memo storage length invalid");
            g_byte_array_append(payload, (unsigned char *)buffer + start + prefix, length - prefix);
            pointer = single ? 0 : mdb_get_int32(buffer, start);
        }
        g_hash_table_destroy(visited);
        if (payload->len != expected) fail("memo chain length mismatch");
    }
    printf("],\"payload\":"); encoded(payload->data, payload->len);
    printf(",\"payload_sha256\":"); digest(payload->data, payload->len); putchar('}');
    g_byte_array_free(payload, TRUE);
}

static void properties(MdbColumn *col) {
    printf("{");
    if (col->props && col->props->hash) {
        GHashTableIter iter; gpointer key, value; unsigned int count = 0;
        g_hash_table_iter_init(&iter, col->props->hash);
        while (g_hash_table_iter_next(&iter, &key, &value)) {
            if (++count > 64 || strlen(key) > 256 || strlen(value) > 16384) fail("column property limit");
            if (count > 1) putchar(',');
            quoted(key); putchar(':'); quoted(value);
        }
    }
    putchar('}');
}

static void table(MdbCatalogEntry *entry, unsigned int *total_rows) {
    MdbTableDef *table = mdb_read_table(entry);
    if (!table || !mdb_read_columns(table) || !table->num_cols || table->num_cols > MAX_COLUMNS || table->num_rows > MAX_ROWS)
        fail("table definition unsupported or exceeds limits");
    printf("{\"kind\":\"table\",\"name\":"); quoted(table->name);
    printf(",\"definition_page\":%lu,\"declared_rows\":%u,\"columns\":[", entry->table_pg, table->num_rows);
    for (unsigned int i = 0; i < table->num_cols; i++) {
        MdbColumn *col = g_ptr_array_index(table->columns, i);
        if (i) putchar(',');
        printf("{\"name\":"); quoted(col->name);
        printf(",\"type\":%d,\"size\":%d,\"precision\":%d,\"scale\":%d,\"fixed\":%s,\"properties\":", col->col_type, col->col_size, col->col_prec, col->col_scale, col->is_fixed ? "true" : "false");
        properties(col); putchar('}');
    }
    printf("]}\n");
    mdb_rewind_table(table); MdbHandle *mdb = entry->mdb;
    MdbField fields[MAX_COLUMNS]; unsigned int rows = 0;
    while (mdb_fetch_row(table)) {
        if (++rows > MAX_ROWS || ++*total_rows > MAX_ROWS) fail("row limit");
        int start; size_t length; unsigned int slot = table->cur_row - 1;
        if (mdb_find_row(mdb, slot, &start, &length) || !length || length > 4096) fail("row storage invalid");
        start &= 0x1fff;
        int count = mdb_crack_row(table, start, length, fields);
        if (count < 0 || (unsigned int)count != table->num_cols) fail("deleted or unsupported column mapping");
        printf("{\"kind\":\"row\",\"table\":"); quoted(table->name);
        printf(",\"ordinal\":%u,\"storage\":", rows);
        storage(table->cur_phys_pg, slot, start, length, mdb->pg_buf + start);
        printf(",\"fields\":[");
        for (unsigned int i = 0; i < table->num_cols; i++) {
            MdbField *field = NULL; MdbColumn *col = g_ptr_array_index(table->columns, i);
            for (int j = 0; j < count; j++) if (fields[j].colnum == (int)i) field = &fields[j];
            if (!field || field->start < 0 || field->siz < 0 || field->start + field->siz > 4096) fail("field mapping invalid");
            if (i) putchar(',');
            printf("[%d,%d,%s,", field->start, field->siz, field->is_null ? "true" : "false");
            if (col->col_type == MDB_BOOL) printf("%s", field->is_null ? "false" : "true");
            else if (field->is_null) printf("null");
            else if (col->col_type == MDB_MEMO) memo(mdb, field);
            else encoded(mdb->pg_buf + field->start, field->siz);
            putchar(']');
        }
        printf("]}\n");
    }
    if (rows != table->num_rows) fail("declared and recovered row counts differ");
    printf("{\"kind\":\"table_end\",\"name\":"); quoted(table->name); printf(",\"rows\":%u}\n", rows);
    mdb_free_tabledef(table);
}

int main(int argc, char **argv) {
    if (argc != 3) fail("expected parent PID and local input path");
    char *end; long parent = strtol(argv[1], &end, 10);
    if (*end || parent <= 1 || parent > INT32_MAX) fail("invalid parent PID");
    isolate((pid_t)parent);
    struct stat info;
    if (lstat(argv[2], &info) || !S_ISREG(info.st_mode) || info.st_size < 4096 || info.st_size > MAX_FILE) fail("input size or type invalid");
    MdbHandle *mdb = mdb_open(argv[2], MDB_NOFLAGS);
    if (!mdb || mdb->f->jet_version != MDB_VER_JET4) fail("only valid JET4 database files are supported");
    if (!mdb_read_catalog(mdb, MDB_ANY) || mdb->num_catalog > 512) fail("catalog unavailable or exceeds limit");
    Dl_info library;
    if (!dladdr((void *)mdb_open, &library) || !library.dli_fname) fail("library provenance unavailable");
    gchar *bytes; gsize length;
    if (!g_file_get_contents(library.dli_fname, &bytes, &length, NULL) || length > 16 * 1024 * 1024) fail("library fingerprint unavailable");
    printf("{\"kind\":\"header\",\"protocol\":\"%s\",\"libmdb_header_version\":\"1.0.0\",\"library_sha256\":", PROTOCOL);
    digest(bytes, length); g_free(bytes);
    printf(",\"jet_version\":%u,\"code_page\":%u,\"page_size\":%zu,\"network_isolated\":true}\n", mdb->f->jet_version, mdb->f->code_page, (size_t)mdb->fmt->pg_size);
    unsigned int tables = 0, rows = 0;
    for (unsigned int i = 0; i < mdb->num_catalog; i++) {
        MdbCatalogEntry *entry = g_ptr_array_index(mdb->catalog, i);
        int system = mdb_is_system_table(entry);
        printf("{\"kind\":\"object\",\"name\":"); quoted(entry->object_name);
        printf(",\"type\":%d,\"system\":%s,\"flags\":%d,\"definition_page\":%lu}\n", entry->object_type, system ? "true" : "false", entry->flags, entry->table_pg);
        if (entry->object_type == MDB_TABLE && !system) {
            if (++tables > MAX_TABLES) fail("table limit");
            table(entry, &rows);
        }
    }
    if (!tables) fail("no user tables");
    printf("{\"kind\":\"end\",\"tables\":%u,\"rows\":%u}\n", tables, rows);
    mdb_close(mdb);
    if (ferror(stdout)) fail("output failed");
    return 0;
}
