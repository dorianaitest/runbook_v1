const path = require('path');
const mongoose = require('mongoose');
const { checkEmailConfig, createInvite } = require('@librechat/api');
const { User } = require('@librechat/data-schemas').createModels(mongoose);
require('module-alias')({ base: path.resolve(__dirname, '..', 'api') });
const { askQuestion, silentExit } = require('./helpers');
const { createToken } = require('~/models');
const { sendEmail } = require('~/server/utils');
const connect = require('./connect');

(async () => {
  await connect();

  console.purple('--------------------------');
  console.purple('Invite a new user account!');
  console.purple('--------------------------');

  let email = '';
  if (process.argv.length >= 3) {
      email = process.argv[2];
  }
  if (!email) {
    email = await askQuestion('Email:');
  }
  if (!email.includes('@')) {
    console.red('Error: Invalid email address!');
    silentExit(1);
  }

  const userExists = await User.findOne({ email });
  if (userExists) {
    console.red('Error: A user with that email already exists!');
    silentExit(1);
  }

  const token = await createInvite(email, { createToken });
  if (token && token.message) {
    console.red('Error: ' + token.message);
    silentExit(1);
  }
  const inviteLink = `${process.env.DOMAIN_CLIENT}/register?token=${token}`;
  const appName = process.env.APP_TITLE || 'LibreChat';

  if (!checkEmailConfig()) {
    console.green('Send this link to the user:', inviteLink);
    silentExit(0);
  }

  try {
    await sendEmail({
      email: email,
      subject: `Invite to join ${appName}!`,
      payload: {
        appName: appName,
        inviteLink: inviteLink,
        year: new Date().getFullYear(),
      },
      template: 'inviteUser.handlebars',
    });
  } catch (error) {
    console.error('Error: ' + error.message);
    silentExit(1);
  }

  console.green('Invitation sent successfully!');
  silentExit(0);
})();

process.on('uncaughtException', (err) => {
  if (!err.message.includes('fetch failed')) {
    console.error('There was an uncaught error:');
    console.error(err);
  }
  if (!err.message.includes('fetch failed')) {
    process.exit(1);
  }
});
