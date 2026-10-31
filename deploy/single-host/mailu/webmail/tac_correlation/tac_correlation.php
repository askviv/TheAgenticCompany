<?php

/**
 * Adds controller-supplied correlation headers to native Roundcube sends.
 * Metadata is accepted only on the dedicated TAC compose route; ordinary
 * human-authored Roundcube messages remain untouched.
 */
class tac_correlation extends rcube_plugin
{
    private $rc;

    public function init()
    {
        $this->rc = rcmail::get_instance();
        $this->rc->output->set_env('tac_logged_in_email', $this->rc->get_user_email());
        $this->add_hook('message_compose', [$this, 'message_compose']);
        $this->add_hook('message_before_send', [$this, 'message_before_send']);
        $this->add_hook('message_sent', [$this, 'message_sent']);
        $this->include_script('tac_correlation.js');
    }

    public function message_compose($args)
    {
        $param = $args['param'] ?? [];
        $correlation = $this->correlation_from_param($param);
        if ($correlation === null) {
            return $args;
        }

        // Roundcube uses this parameter as Message-ID in its standard compose
        // implementation. This keeps its normal SMTP and Sent-folder path.
        $args['param']['message-id'] = $correlation['message_id'];
        $compose_id = $args['id'] ?? '';
        if (!is_string($compose_id) || $compose_id === '') {
            return $args;
        }
        $_SESSION['tac_correlation_' . $compose_id] = $correlation;
        $this->rc->output->set_env('tac_correlation', $correlation);
        return $args;
    }

    public function message_before_send($args)
    {
        $message_id = $this->post_value('_tac_message_id');
        $run_id = $this->post_value('_tac_run_id');
        $generation = $this->post_value('_tac_generation');
        $in_reply_to = $this->post_value('_tac_in_reply_to');
        $reply_to = $this->post_value('_tac_reply_to');
        $automated = $this->post_value('_tac_automated');
        $compose_id = $this->post_value('_id');
        $expected = $compose_id === '' ? null : ($_SESSION['tac_correlation_' . $compose_id] ?? null);

        // Non-TAC Roundcube messages have no metadata and pass through
        // unchanged. Any TAC fields without the server-side compose context
        // are rejected, including fields added to an ordinary Reply form.
        if (!is_array($expected)) {
            if ($message_id === '' && $run_id === '' && $generation === ''
                && $in_reply_to === '' && $reply_to === '' && $automated === '') {
                return $args;
            }
            return $this->abort($args, 'TAC compose context is missing or expired.');
        }
        if ($message_id !== ($expected['message_id'] ?? null)
                || $run_id !== ($expected['run_id'] ?? null)
                || $generation !== ($expected['generation'] ?? null)
                || $in_reply_to !== ($expected['in_reply_to'] ?? null)
                || $reply_to !== ($expected['reply_to'] ?? null)
                || $automated !== ($expected['automated'] ?? null)) {
            return $this->abort($args, 'TAC compose correlation metadata changed.');
        }
        if (!$this->valid_message_id($message_id)
            || !$this->valid_run_id($run_id)
            || !preg_match('/^(0|[1-9][0-9]*)$/D', $generation)
            || ($in_reply_to !== '' && !$this->valid_message_id($in_reply_to))) {
            return $this->abort($args, 'Invalid TAC message correlation metadata.');
        }

        $headers = $args['message']->headers();
        if (($headers['Message-ID'] ?? null) !== $message_id) {
            return $this->abort($args, 'TAC Message-ID did not match the composed message.');
        }

        $outgoing = [
            'X-TAC-Run-ID' => $run_id,
            'X-TAC-Generation' => $generation,
        ];
        if ($reply_to !== '') {
            if (!filter_var($reply_to, FILTER_VALIDATE_EMAIL)) {
                return $this->abort($args, 'TAC Reply-To address is invalid.');
            }
            $outgoing['Reply-To'] = $reply_to;
        }
        if ($automated === '1') {
            $outgoing['Auto-Submitted'] = 'auto-replied';
        }
        if ($in_reply_to !== '') {
            $outgoing['In-Reply-To'] = $in_reply_to;
            $outgoing['References'] = $in_reply_to;
        }
        $args['message']->headers($outgoing, true);
        return $args;
    }

    public function message_sent($args)
    {
        $compose_id = $this->post_value('_id');
        if ($compose_id !== '') {
            $this->rc->session->remove('tac_correlation_' . $compose_id);
        }
        return $args;
    }

    private function correlation_from_param($param)
    {
        $message_id = $param['tac_message_id'] ?? '';
        $run_id = $param['tac_run_id'] ?? '';
        $generation = $param['tac_generation'] ?? '';
        $in_reply_to = $param['tac_in_reply_to'] ?? '';
        $reply_to = $param['tac_reply_to'] ?? '';
        $automated = $param['tac_automated'] ?? '0';
        if ($message_id === '' && $run_id === '' && $generation === '' && $in_reply_to === ''
            && $reply_to === '' && $automated === '0') {
            return null;
        }
        if (!$this->valid_message_id($message_id)
            || !$this->valid_run_id($run_id)
            || !preg_match('/^(0|[1-9][0-9]*)$/D', (string) $generation)
            || ($in_reply_to !== '' && !$this->valid_message_id($in_reply_to))
            || ($reply_to !== '' && !filter_var($reply_to, FILTER_VALIDATE_EMAIL))
            || !in_array((string) $automated, ['0', '1'], true)) {
            return null;
        }
        return [
            'message_id' => $message_id,
            'run_id' => $run_id,
            'generation' => (string) $generation,
            'in_reply_to' => $in_reply_to,
            'reply_to' => $reply_to,
            'automated' => (string) $automated,
        ];
    }

    private function post_value($name)
    {
        return rcube_utils::get_input_string($name, rcube_utils::INPUT_POST, true) ?? '';
    }

    private function valid_message_id($value)
    {
        // Match the controller's established Message-ID contract while rejecting
        // nested brackets and whitespace that could inject another header.
        return is_string($value) && preg_match('/^<[^<>\s]+>$/D', $value);
    }

    private function valid_run_id($value)
    {
        return is_string($value) && $value !== ''
            && !preg_match('/[\x00-\x1F\x7F]/', $value);
    }

    private function abort($args, $error)
    {
        $args['abort'] = true;
        $args['error'] = $error;
        return $args;
    }
}
