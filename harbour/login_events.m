// Native macOS 13+ authentication notification source. No AUTH decisions,
// process-execution subscriptions, file monitoring, or eslogger dependency.
// Build/signing prerequisites are documented in LOGIN_COLLECTOR.md.
#import <Foundation/Foundation.h>
#import <EndpointSecurity/EndpointSecurity.h>
#import <bsm/libbsm.h>
#import <dispatch/dispatch.h>
#import <signal.h>

static NSString *text(es_string_token_t value) {
    if (!value.data || !value.length) return @"";
    return [[NSString alloc] initWithBytes:value.data length:MIN(value.length, 512) encoding:NSUTF8StringEncoding] ?: @"";
}
static void emit(NSDictionary *event) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:event options:0 error:nil];
    if (data) { fwrite(data.bytes, 1, data.length, stdout); fputc('\n', stdout); fflush(stdout); }
}
static void handle(const es_message_t *msg) {
    @autoreleasepool {
        static uint64_t last = 0;
        if (msg->version >= 4) {
            if (last && msg->global_seq_num > last + 1)
                emit(@{@"collector_error": @"Endpoint Security dropped events; collection is incomplete."});
            last = msg->global_seq_num;
        }
        NSMutableDictionary *out = [@{@"occurred_at": @(msg->time.tv_sec + msg->time.tv_nsec/1e9),
            @"pid": @(audit_token_to_pid(msg->process->audit_token)), @"event_type": @"authentication",
            @"service": @"os-authentication", @"result": @"unknown"} mutableCopy];
        switch (msg->event_type) {
            case ES_EVENT_TYPE_NOTIFY_OPENSSH_LOGIN: {
                es_event_openssh_login_t *e = msg->event.openssh_login;
                out[@"service"] = @"ssh"; out[@"username"] = text(e->username);
                out[@"source_ip"] = text(e->source_address);
                out[@"result"] = e->success ? @"success" : @"failure";
                if (e->has_uid) out[@"uid"] = @(e->uid.uid);
                switch (e->result_type) {
                    case ES_OPENSSH_INVALID_USER: out[@"result"] = @"invalid_user"; break;
                    case ES_OPENSSH_LOGIN_EXCEED_MAXTRIES: case ES_OPENSSH_LOGIN_ROOT_DENIED: out[@"result"] = @"rejected"; break;
                    case ES_OPENSSH_AUTH_FAIL_PASSWD: out[@"method"] = @"password"; break;
                    case ES_OPENSSH_AUTH_FAIL_PUBKEY: out[@"method"] = @"publickey"; break;
                    case ES_OPENSSH_AUTH_FAIL_KBDINT: out[@"method"] = @"keyboard-interactive"; break;
                    case ES_OPENSSH_AUTH_FAIL_HOSTBASED: out[@"method"] = @"hostbased"; break;
                    case ES_OPENSSH_AUTH_FAIL_GSSAPI: out[@"method"] = @"gssapi"; break;
                    default: break; // A successful ES event does not expose the SSH method/key.
                }
                out[@"native_result"] = @(e->result_type);
                break;
            }
            case ES_EVENT_TYPE_NOTIFY_OPENSSH_LOGOUT: {
                es_event_openssh_logout_t *e = msg->event.openssh_logout;
                out[@"service"] = @"ssh"; out[@"username"] = text(e->username); out[@"uid"] = @(e->uid);
                out[@"source_ip"] = text(e->source_address); out[@"event_type"] = @"disconnect"; out[@"result"] = @"disconnected";
                break;
            }
            case ES_EVENT_TYPE_NOTIFY_LOGIN_LOGIN: {
                es_event_login_login_t *e = msg->event.login_login;
                out[@"service"] = @"login"; out[@"username"] = text(e->username);
                out[@"result"] = e->success ? @"success" : @"failure";
                if (e->has_uid) out[@"uid"] = @(e->uid.uid);
                out[@"evidence"] = text(e->failure_message); break;
            }
            case ES_EVENT_TYPE_NOTIFY_LOGIN_LOGOUT: {
                es_event_login_logout_t *e = msg->event.login_logout;
                out[@"service"] = @"login"; out[@"username"] = text(e->username); out[@"uid"] = @(e->uid);
                out[@"event_type"] = @"session_end"; out[@"result"] = @"closed"; break;
            }
#define GRAPHICAL(kind, field, type, result) case kind: { \
                out[@"service"] = @"graphical-login"; out[@"event_type"] = type; out[@"result"] = result; \
                out[@"username"] = text(msg->event.field->username); \
                out[@"session_id"] = @(msg->event.field->graphical_session_id); break; }
            GRAPHICAL(ES_EVENT_TYPE_NOTIFY_LW_SESSION_LOGIN, lw_session_login, @"session_start", @"opened")
            GRAPHICAL(ES_EVENT_TYPE_NOTIFY_LW_SESSION_LOGOUT, lw_session_logout, @"session_end", @"closed")
            GRAPHICAL(ES_EVENT_TYPE_NOTIFY_LW_SESSION_LOCK, lw_session_lock, @"screen_lock", @"locked")
            GRAPHICAL(ES_EVENT_TYPE_NOTIFY_LW_SESSION_UNLOCK, lw_session_unlock, @"screen_unlock", @"unlocked")
#undef GRAPHICAL
            case ES_EVENT_TYPE_NOTIFY_AUTHENTICATION: {
                es_event_authentication_t *e = msg->event.authentication;
                out[@"result"] = e->success ? @"success" : @"failure";
                switch (e->type) {
                    case ES_AUTHENTICATION_TYPE_OD:
                        out[@"method"] = @"open-directory"; out[@"username"] = text(e->data.od->record_name);
                        out[@"directory"] = text(e->data.od->node_name); break;
                    case ES_AUTHENTICATION_TYPE_TOUCHID:
                        out[@"method"] = @"touch-id";
                        if (e->data.touchid->has_uid) out[@"uid"] = @(e->data.touchid->uid.uid); break;
                    case ES_AUTHENTICATION_TYPE_TOKEN:
                        out[@"method"] = @"token"; out[@"token_id"] = text(e->data.token->token_id);
                        out[@"token_public_key_hash"] = text(e->data.token->pubkey_hash);
                        out[@"kerberos_principal"] = text(e->data.token->kerberos_principal); break;
                    case ES_AUTHENTICATION_TYPE_AUTO_UNLOCK:
                        out[@"method"] = @"apple-watch"; out[@"username"] = text(e->data.auto_unlock->username); break;
                    default: break;
                }
                break;
            }
            case ES_EVENT_TYPE_NOTIFY_SCREENSHARING_ATTACH: {
                es_event_screensharing_attach_t *e = msg->event.screensharing_attach;
                out[@"service"] = @"screen-sharing"; out[@"result"] = e->success ? @"success" : @"failure";
                out[@"source_ip"] = text(e->source_address); out[@"method"] = text(e->authentication_type);
                out[@"username"] = text(e->authentication_username); out[@"session_username"] = text(e->session_username);
                out[@"viewer_appleid"] = text(e->viewer_appleid); out[@"session_id"] = @(e->graphical_session_id); break;
            }
            case ES_EVENT_TYPE_NOTIFY_SCREENSHARING_DETACH: {
                es_event_screensharing_detach_t *e = msg->event.screensharing_detach;
                out[@"service"] = @"screen-sharing"; out[@"event_type"] = @"disconnect"; out[@"result"] = @"disconnected";
                out[@"source_ip"] = text(e->source_address); out[@"viewer_appleid"] = text(e->viewer_appleid);
                out[@"session_id"] = @(e->graphical_session_id); break;
            }
            default: return;
        }
        emit(out);
    }
}
int main(void) {
    @autoreleasepool {
        if (@available(macOS 13.0, *)) {
            es_client_t *client = NULL;
            es_new_client_result_t result = es_new_client(&client, ^(es_client_t *c, const es_message_t *msg) { (void)c; handle(msg); });
            if (result != ES_NEW_CLIENT_RESULT_SUCCESS) {
                emit(@{@"collector_error": [NSString stringWithFormat:@"Endpoint Security initialization failed (%d). Check root, signing entitlement and Full Disk Access.", result]});
                return 1;
            }
            es_event_type_t events[] = { ES_EVENT_TYPE_NOTIFY_OPENSSH_LOGIN, ES_EVENT_TYPE_NOTIFY_OPENSSH_LOGOUT,
                ES_EVENT_TYPE_NOTIFY_LOGIN_LOGIN, ES_EVENT_TYPE_NOTIFY_LOGIN_LOGOUT,
                ES_EVENT_TYPE_NOTIFY_LW_SESSION_LOGIN, ES_EVENT_TYPE_NOTIFY_LW_SESSION_LOGOUT,
                ES_EVENT_TYPE_NOTIFY_LW_SESSION_LOCK, ES_EVENT_TYPE_NOTIFY_LW_SESSION_UNLOCK,
                ES_EVENT_TYPE_NOTIFY_AUTHENTICATION, ES_EVENT_TYPE_NOTIFY_SCREENSHARING_ATTACH, ES_EVENT_TYPE_NOTIFY_SCREENSHARING_DETACH };
            if (es_subscribe(client, events, sizeof(events)/sizeof(events[0])) != ES_RETURN_SUCCESS) {
                emit(@{@"collector_error": @"Endpoint Security subscription failed."}); es_delete_client(client); return 1;
            }
            emit(@{@"ready": @YES});
            dispatch_main();
        } else {
            emit(@{@"collector_error": @"Native authentication events require macOS 13 or later."});
            return 1;
        }
    }
}
