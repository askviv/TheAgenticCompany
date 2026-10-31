/* Attach the compose correlation values to Roundcube's normal send form. */
window.tacCorrelationPluginLoaded = true;

rcmail.addEventListener('init', function () {
  const correlation = rcmail.env.tac_correlation;
  const form = document.querySelector('#form');
  if (!correlation || !form) {
    return;
  }

  const values = {
    _tac_message_id: correlation.message_id,
    _tac_run_id: correlation.run_id,
    _tac_generation: correlation.generation,
    _tac_in_reply_to: correlation.in_reply_to,
    _tac_reply_to: correlation.reply_to,
    _tac_automated: correlation.automated,
  };
  for (const [name, value] of Object.entries(values)) {
    const input = document.createElement('input');
    input.type = 'hidden';
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }
  form.dataset.tacCorrelationReady = 'true';
});
