# Hyperparameter tuning
# Input parameters (-m model, -t trials, -f features)
# Class balancing
# Feature infusion

import loadembeddings
import loadfeatures
import buildmodel

import argparse
import numpy as np
import pandas as pd
import tensorflow as tf
import re, random, os
import kerastuner as kt
import sys
import copy

from kerastuner.engine import tuner_utils
from tensorflow.keras import preprocessing
from statistics import mean, median
from tensorflow import keras
from tensorflow.keras import layers, initializers
from kerastuner.tuners import RandomSearch
from tensorflow.keras.callbacks import EarlyStopping
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.utils import class_weight
from gensim.models import KeyedVectors
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.model_selection import StratifiedKFold

# Tune hyperparameters
class CVTuner(kt.Tuner):
  def run_trial(self, trial, x, y, *fit_args, **fit_kwargs):
    print('Running trial: ' + str(trial.trial_id))

    # Handle any callbacks passed to `fit`.
    copied_fit_kwargs = copy.copy(fit_kwargs)
    callbacks = fit_kwargs.pop('callbacks', [])
    callbacks = self._deepcopy_callbacks(callbacks)
    self._configure_tensorboard_dir(callbacks, trial.trial_id)
    callbacks.append(tuner_utils.TunerCallback(self, trial))
    copied_fit_kwargs['callbacks'] = callbacks
    
    # Batch size
    hp = trial.hyperparameters
    batch_size = hp.Choice('batch_size', values=[8, 16, 32, 64, 128, 256])

    # K-Fold Cross Validator model evaluation
    fold_no = 1
    num_folds = 10

    # Statistics per fold
    objective = []
    f1_per_fold = []
    precision_per_fold = []
    recall_per_fold = []

    # Define the K-Fold Cross Validator (Stratifield helps with imbalance)
    kfold = StratifiedKFold(n_splits=num_folds, shuffle=False)
    
    # Merge both dataframes so KFold can split them
    emb_x = pd.DataFrame(x[0]).T
    lex_x = x[1].T
    x = emb_x.append(lex_x, ignore_index=True)
    x = x.T.to_numpy()

    print(type(x))
    print(x.shape)
    # Perform CV
    for train, dev in kfold.split(x,y):
      print('--------------------------------')
      print(f'Training for fold {fold_no} ...')

      x_train, x_dev = x[train], x[dev]
      y_train, y_dev = y[train], y[dev]
      
      # Undo the embeding-lexicon merge for train
      split_train = np.split(x_train, [75], axis=1)
      emb_train = split_train[0]
      lex_train = split_train[1]

      # Undo the embeding-lexicon merge for dev
      split_dev = np.split(x_dev, [75], axis=1)
      emb_dev = split_dev[0]
      lex_dev = split_dev[1]

      # Train the model with the new HP
      model = self.hypermodel.build(hp)
      model.fit([emb_train, lex_train], y_train, batch_size=batch_size, *fit_args, **copied_fit_kwargs)

      # Store objective metric for this fold
      objective.append(model.evaluate([emb_dev, lex_dev], y_dev))
      
      # Calculate precision, recall and f1
      y_prob = model.predict([emb_dev, lex_dev], batch_size=128, verbose=0)
      y_classes = np.around(y_prob, decimals=0)
      y_pred = y_classes.astype(int)
      precision_per_fold.append(precision_score(y_dev, y_pred, average="macro"))
      recall_per_fold.append(recall_score(y_dev, y_pred, average="macro"))
      f1_per_fold.append(f1_score(y_dev, y_pred, average="macro"))

      fold_no = fold_no + 1

    # Update and save trial
    self.oracle.update_trial(trial.trial_id, {'accuracy': np.mean(objective)})
    self.save_model(trial.trial_id, model)
    print("----------------------------------------------")
    print("Average scores for all folds:")
    print(f"> Precision macro: {np.mean(precision_per_fold)}")
    print(f"> Recall macro: {np.mean(recall_per_fold)}")
    print(f"> F1 macro: {np.mean(f1_per_fold)}")
    print("----------------------------------------------")
    
def main(args):
  print("Version", tf.__version__)
  print("Device", tf.test.gpu_device_name())
  print("GPUS", tf.config.list_physical_devices('GPU'))

  # Data loading
  path = '../data/HaterNet/'

  training_set = pd.read_csv(path + 'train_prep_uncased.tsv', sep='\t')
  test_set = pd.read_csv(path + 'test_prep_uncased.tsv', sep='\t')

  # Remove accents from train
  x_train = training_set.text
  y_train = training_set.label
  
  # Remove accents from test
  x_test = test_set.text
  y_test = test_set.label
  
  # Normalize dataset for the lexicon matching
  norm_train = training_set.text.str.normalize('NFKD').str.encode('ascii', errors='ignore').str.decode('utf-8')
  norm_test = test_set.text.str.normalize('NFKD').str.encode('ascii', errors='ignore').str.decode('utf-8')

  # Lexicon loading
  if args.lexicon == 'sel':
    lexicon = loadfeatures.SEL(path='../lexicons/')
    lex_train = lexicon.process(dataset=norm_train)
    lex_test = lexicon.process(dataset=norm_test)
  elif args.lexicon == 'liwc':
    lexicon = loadfeatures.SpanishLIWC(path='../lexicons/')
    lex_train = lexicon.process(dataset=norm_train)
    lex_test = lexicon.process(dataset=norm_test)
  elif args.lexicon == 'all':
    lexicon = loadfeatures.All(path='../lexicons/')
    lex_train = lexicon.process(dataset=norm_train)
    lex_test = lexicon.process(dataset=norm_test)
  else:
    print("No se utilizará lexicon.")
    
  # Store each tweet as a list of tokens
  token_list = []
  for text in x_train:
    token_list.append(preprocessing.text.text_to_word_sequence(text))

  # For each list of tokens, store its length
  len_texts = []
  for index, tweet in enumerate(token_list):
    len_texts.append(len(tweet))

  # Tokenize
  max_words = 10000   # Top most frequent words
  max_seq = 75       # Size to be padded to (should be greater than the max value=70)

  # Create a tokenize that takes the 10000 most common words
  tokenizer = preprocessing.text.Tokenizer(num_words=max_words)

  # Fit the tokenizer to the dataset
  tokenizer.fit_on_texts(x_train)

  # Dictionary ordered by total frequency
  word_index = tokenizer.word_index
  vocab_size = len(word_index) + 1

  # Transform each tweet into a numerical sequence
  train_sequences = tokenizer.texts_to_sequences(x_train)
  test_sequences = tokenizer.texts_to_sequences(x_test)

  # Fill each sequence with zeros until max_seq
  x_train = preprocessing.sequence.pad_sequences(train_sequences, maxlen=max_seq)
  x_test = preprocessing.sequence.pad_sequences(test_sequences, maxlen=max_seq)

  # Load embeddings
  path = '../embeddings/embeddings-l-model.vec'
  EMB_DIM = 300
  LIMIT = 100000
  embedding_matrix = loadembeddings.load_suc(path, word_index, EMB_DIM, LIMIT)

  # Metrics
  METRICS=[
    keras.metrics.BinaryAccuracy(name='accuracy'),
    keras.metrics.Precision(name='precision'),
    keras.metrics.Recall(name='recall'),
    keras.metrics.AUC(name='auc')
  ]

  
  if args.lexicon:
    model = buildmodel.LSTMFeaturesModel(vocab_size, max_seq, embedding_matrix, EMB_DIM, args.lexicon, METRICS)

  else:
    # Create a model instance for the tuner
    if args.model == 'lstm':
      model = buildmodel.LSTMModel(vocab_size, max_seq, embedding_matrix, EMB_DIM, METRICS)
    elif args.model == 'bilstm':
      model = buildmodel.BiLSTMModel(vocab_size, max_seq, embedding_matrix, EMB_DIM, METRICS)
    elif args.model == 'cnn':
      model = buildmodel.CNNModel(vocab_size, max_seq, embedding_matrix, EMB_DIM, METRICS)
    else:
      print("Wrong model. Please, choose another one.")
      exit()
  
  # Create the tuner
  tuner = CVTuner(
      hypermodel=model,                                             # Model's function name
      oracle=kt.oracles.BayesianOptimization(
        objective=kt.Objective("accuracy", direction="max"),         # Optimizing metric
        max_trials=args.trials                                      # Number of trials, default=10
      ),  
      directory='../hp_trials/',                                    # Directory to store the models
      project_name=args.model + "_" + args.lexicon,                 # Project name
      overwrite=True)                                               # Overwrite the project

  '''
  class_weights = class_weight.compute_class_weight('balanced',
                                                  np.unique(y_train),
                                                  y_train)

  class_weights = dict(enumerate(class_weights))
  '''

  # Early stopping and tensorboard callbacks for fitting
  callbacks = [
      EarlyStopping(monitor='loss', verbose=1, patience=5)
  ]

  print("Searching...")
  if args.lexicon:
    tuner.search(x=[x_train, lex_train], y=y_train, verbose=0, callbacks=callbacks, epochs=10)
  else:
    tuner.search(x=x_train, y=y_train, verbose=0, callbacks=callbacks, epochs=10)

  # Save the best model
  best_model = tuner.get_best_models(num_models=1)
  print(tuner.results_summary(num_trials=1))

  # Statistics
  if args.lexicon:
    y_prob = best_model[0].predict([np.array(x_test), lex_test], batch_size=128, verbose=1)
  else:
    y_prob = best_model[0].predict(np.array(x_test), batch_size=128, verbose=1)
    
  y_classes = np.around(y_prob, decimals=0)
  y_pred = y_classes.astype(int)

  print('\nCLASSIFICATION REPORT\n')
  print(classification_report(y_test, y_pred, digits=4))

  print('\nCONFUSION MATRIX\n')
  print(confusion_matrix(y_test, y_pred))

  print("\nParameters used:")
  print(args.model + " model")
  print(str(args.trials) + " trials")
  print(args.lexicon + " lexicon")
  
if __name__ == "__main__":
  
  # Use the command below before running this script
  # in order to guarantee reproducibility
  # export PYTHONHASHSEED=0
  
  seed = 1
  np.random.seed(seed)
  random.seed(seed)
  tf.random.set_seed(seed)

  # Args parse
  ap = argparse.ArgumentParser()

  ap.add_argument("-m", 
                  "--model",
                  choices=['lstm','bilstm','cnn'],
                  default='lstm',
                  help="Model to be built")

  ap.add_argument("-t",
                  "--trials",
                  type=int,
                  default=10,
                  help="Number of trials")

  ap.add_argument("-l",
                  "--lexicon",
                  choices=['liwc', 'sel', 'all'],
                  default=None,
                  help="Name of the lexicon to infuse")


  args = ap.parse_args()
  main(args)