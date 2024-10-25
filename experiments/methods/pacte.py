from collections import Counter
import numpy as np
import scipy.sparse as sp
import sys

from torch.utils.data import Dataset
from torch.utils.data import DataLoader
import numpy as np
import os
import torch
import torch.nn as nn
import pickle
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score

def sent_to_words(sentences, min_len=2, max_len=15):
    # tokenize words
    import gensim
    for sentence in sentences:
        yield gensim.utils.simple_preprocess(str(sentence), deacc=True, 
                                            min_len=min_len, max_len=max_len)  # deacc=True removes punctuations


def remove_stopwords(texts, default='english', extensions=None):
    # nltk.download('stopwords')
    from nltk.corpus import stopwords
    stop_words = []
    if default is not None:
        stop_words.extend(stopwords.words(default))
    if extensions is not None:
        stop_words.extend(extensions)
    import gensim
    return [[word for word in gensim.utils.simple_preprocess(str(doc)) if word not in stop_words] for doc in texts]


def make_bigrams(data_words):
    import gensim
    bigram = gensim.models.Phrases(data_words, min_count=5, threshold=100) # higher threshld fewer phrases
    bigram_mod = gensim.models.phrases.Phraser(bigram)
    return [bigram_mod[doc] for doc in data_words], bigram, bigram_mod


def make_trigrams(data_words, bigram, bigram_mod):
    import gensim
    trigram = gensim.models.Phrases(bigram[data_words], threshold=100)
    trigram_mod = gensim.models.phrases.Phraser(trigram)
    return [trigram_mod[bigram_mod[doc]] for doc in data_words], trigram, trigram_mod


def lemmatization(texts, allowed_postags=['NOUN', 'ADJ', 'VERB', 'ADV']):
    '''
    Lemmatization for LDA topic modeling.
    '''
    import spacy
    """https://spacy.io/api/annotation"""
    # Initialize spacy 'en' model, keeping only tagger component (for efficiency)
    # python3 -m spacy download en
    nlp = spacy.load('en_core_web_sm', disable=['parser', 'ner'])
    texts_out = []
    for sent in texts:
        doc = nlp(" ".join(sent))
        # do lemmatization and only keep the types of tokens in allowed_postags
        texts_out.append([token.lemma_ for token in doc if token.pos_ in allowed_postags])
    return texts_out


def lemmatization2(texts, allowed_postags=['NOUN', 'ADJ', 'VERB', 'ADV']):
    '''
    Lemmatization for BERT. 
    Although BERT has its own tokenizer, we need match the words for BERT and LDA.
    '''
    import spacy
    """https://spacy.io/api/annotation"""
    # Initialize spacy 'en' model, keeping only tagger component (for efficiency)
    # python3 -m spacy download en
    nlp = spacy.load('en_core_web_sm', disable=['parser', 'ner'])
    texts_out = []
    for sent in texts:
        doc = nlp(" ".join(sent))
        # for tokens whose types in allowed_postages do lemmatization otherwise keep the original form
        texts_out.append([str(token.lemma_) if token.pos_ in allowed_postags else token for token in doc])
    return texts_out


def lemmatization3(texts):
    '''
    Lemmatization for leave-out estimator
    '''
    import spacy
    """https://spacy.io/api/annotation"""
    # Initialize spacy 'en' model, keeping only tagger component (for efficiency)
    # python3 -m spacy download en
    nlp = spacy.load('en_core_web_sm', disable=['parser', 'ner'])
    texts_out = []
    for sent in texts:
        doc = nlp(" ".join(sent))
        # for all tokens do lemmatization and keep all tokens
        texts_out.append([str(token.lemma_) for token in doc])
    return texts_out

def create_dict_corpus(data_words):
    import gensim.corpora as corpora
    
    # Create Dictionary
    id2word = corpora.Dictionary(data_words)

    # Create Corpus
    texts = data_words

    # Term Document Frequency
    corpus = [id2word.doc2bow(text) for text in texts]

    return corpus, id2word

def preprocessing_lda(data):
    import re
    
    # Remove Emails
    data = [re.sub('\S*@\S*\s?', '', sent) for sent in data]

    # Remove new line characters
    data = [re.sub('\s+', ' ', sent) for sent in data]

    # Remove distracting single quotes
    data = [re.sub("\'", "", sent) for sent in data]

    # tokenize words and clean-up text
    data_words = list(sent_to_words(data))

    # remove stop words
    # need to remove the news source names
    data_words_nostops = remove_stopwords(data_words, 
                                          extensions=['from', 'subject', 're', 'edu', 
                                                       'use', 'rt', 'cnn', 'fox', 'huffington', 'breitbart'])

    # form bigrams
    data_words_bigrams, _, _ = make_bigrams(data_words_nostops)

    #  do lemmatization keeping only noun, adj, vb, adv, propnoun
    # other tokens are not useful for topic modeling
    data_lematized = lemmatization(data_words_bigrams, allowed_postags=['NOUN', 'ADJ', 'VERB', 'ADV', 'PROPN'])
    
    corpus, id2word = create_dict_corpus(data_lematized)

    return data_lematized, corpus, id2word

def preprocessing_bert(data):
    import re
    
    # Remove Emails
    data = [re.sub('\S*@\S*\s?', '', sent) for sent in data]

    # Remove new line characters
    data = [re.sub('\s+', ' ', sent) for sent in data]

    # tokenize words and clean-up text
    data_words = list(sent_to_words(data,min_len=1, max_len=30))

    # remove stop words
    data_words_nostops = remove_stopwords(data_words, default=None,
                                          extensions=['cnn', 'fox', 'huffington', 'breitbart'])

    # form bigrams
    data_words_bigrams, _, _ = make_bigrams(data_words)

    #  do lemmatization for only noun, adj, vb, adv propnoun, following the lemmatization for LDA
    #  keep the others which will be used as context
    data_lematized = lemmatization2(data_words_bigrams, allowed_postags=['NOUN', 'ADJ', 'VERB', 'ADV', 'PROPN'])
    
    return data_lematized




class NewsDataset(Dataset):
    def __init__(self, texts, labels, topic_masks):
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained('bert-base-uncased')
        encodings = tokenizer(texts, truncation=True, padding=True)
        self.encodings = encodings
        self.labels = labels
        self.topic_masks = topic_masks

    def __getitem__(self, idx):
        item = {key: torch.tensor(val[idx]) for key, val in self.encodings.items()}
        item['labels'] = torch.tensor(self.labels[idx])
        item['topic_masks'] = torch.tensor(self.topic_masks[idx])
        return item

    def __len__(self):
        return len(self.labels)


class FC(nn.Module):
    def __init__(self, n_in, n_out):
        super(FC, self).__init__()
        self.fc = nn.Linear(n_in, n_out)

    def forward(self, x):
        return self.fc(x)


class Engine:
    def __init__(self, args):
        # gpu
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu
        device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
        os.makedirs('ckp', exist_ok=True)

        # dataset
        print('Loading data....')
        texts_processed = pd.Series(pickle.load(open(os.path.join(args.data_path, 'texts_processed_bert.pkl'), 'rb')))
        texts = texts_processed.apply(lambda x: ' '.join(x))
        df_news = pd.read_csv(os.path.join(args.data_path, 'df_news.csv'))
        # only making the model differentiate between left and right
        labels = df_news['source'].map({'cnn': 0, 'fox': 1, 'huff': 0, 'breit': 1, 'nyt': 0, 'nyp': 1})
        if args.shuffle: # shuffle the labels to serve as the baseline, where the languge model cannot learn partisanship
            labels = labels.sample(frac=1)
        del df_news
        topic_masks = pd.Series(pickle.load(open(os.path.join(args.data_path, 'topic_masks.pkl'), 'rb')))
        val_idexes = pickle.load(open(os.path.join(args.data_path, 'idxes_val.pkl'), 'rb'))
        train_idexes = set(list(range(len(texts_processed)))) - val_idexes
        # train_idexes = range(len(texts_processed))
        # val_idexes = range(len(texts_processed))

        train_idexes = np.array(list(train_idexes))
        val_idexes = np.array(list(val_idexes))
        train_mask = np.isin(np.arange(len(texts_processed)), train_idexes)
        val_mask = np.isin(np.arange(len(texts_processed)), val_idexes)
        print('Done.')

        texts_train = texts[train_mask].tolist()
        texts_val = texts[val_mask].tolist()
        texts = texts.tolist()
        labels_train = labels[train_mask].tolist()
        labels_val = labels[val_mask].tolist()
        labels = labels.tolist()
        topic_masks_train = topic_masks[train_mask].tolist()
        topic_masks_val = topic_masks[val_mask].tolist()
        topic_masks = topic_masks.tolist()
        print('Done\n')

        if args.init_train:
            print('Preparing dataset....')
            train_dataset = NewsDataset(texts_train, labels_train, topic_masks_train)
            train_loader = DataLoader(train_dataset, batch_size=args.batch_size, shuffle=True)
            val_dataset = NewsDataset(texts_val, labels_val, topic_masks_val)
            val_loader = DataLoader(val_dataset, batch_size=args.batch_size)
            dataset = NewsDataset(texts, labels, topic_masks)
            loader = DataLoader(dataset, batch_size=int(1.5*args.batch_size))

            print('Done\n')

            # model
            print('Initializing model....')
            from transformers import AutoModelForSequenceClassification, AdamW
            model = AutoModelForSequenceClassification.from_pretrained('bert-base-uncased', num_labels=2)

            print('Done\n')
            print("Let's use", torch.cuda.device_count(), "GPUs!")
            model = nn.DataParallel(model)
            model.to(device)
            optimizer = AdamW(model.parameters(), lr=args.lr, weight_decay=5e-4)
        os.makedirs('ckp', exist_ok=True)

        if not args.shuffle:
            model_path = f'ckp/model.pt'
        else:
            model_path = f'ckp/model_shuffle.pt'

        self.device = device
        if args.init_train:
            self.model = model
            self.optimizer = optimizer
            self.train_loader = train_loader
            self.val_loader = val_loader
            self.loader = loader
        self.texts = texts
        self.labels = labels
        self.model_path = model_path
        self.args = args

    def train(self):

        if (not os.path.exists(self.model_path)) and (not self.args.unfinetuned):
            best_epoch_loss = float('inf')
            best_epoch_f1 = 0
            best_epoch = 0
            import copy
            best_state_dict = copy.deepcopy(self.model.state_dict())
            for epoch in range(self.args.epochs):
                print(f"{'*' * 20}Epoch: {epoch + 1}{'*' * 20}")
                loss = self.train_epoch()
                acc, f1 = self.eval()

                if f1 > best_epoch_f1:
                    best_epoch = epoch
                    best_epoch_loss = loss
                    best_epoch_f1 = f1
                    best_state_dict = copy.deepcopy(self.model.state_dict())
                print(
                    f'Epoch {epoch + 1}, Loss: {loss:.3f}, Acc: {acc:.3f}, F1: {f1:.3f}, '
                    f'Best Epoch:{best_epoch + 1}, '
                    f'Best Epoch F1: {best_epoch_f1:.3f}\n')

                if epoch - best_epoch >= 5:
                    break

            print('Saving the best checkpoint....')
            torch.save(best_state_dict, self.model_path)
            print(
                f'Best Epoch: {best_epoch + 1}, Best Epoch F1: {best_epoch_f1:.3f}, Best Epoch Loss: {best_epoch_loss:.3f}')
        self.calc_embeddings(True)
        self.calc_embeddings()

    def train_epoch(self):
        self.model.train()
        epoch_loss = 0
        for i, batch in enumerate(self.train_loader):
            self.optimizer.zero_grad()
            input_ids = batch['input_ids'].to(self.device)
            attention_mask = batch['attention_mask'].to(self.device)
            labels = batch['labels'].to(self.device)
            outputs = self.model(input_ids, attention_mask=attention_mask, labels=labels, output_hidden_states=True)
            loss = outputs[0].mean()
            loss.backward()
            self.optimizer.step()
            epoch_loss += loss.item()
            if i % (len(self.train_loader) // 20) == 0:
                print(f'Batch: {i + 1}/{len(self.train_loader)}\tloss:{loss.item():.3f}')

        return epoch_loss / len(self.train_loader)

    def eval(self):
        self.model.eval()
        y_pred = []
        y_true = []
        print('Evaluating f1....')
        with torch.no_grad():
            for i, batch in enumerate(self.val_loader):
                input_ids = batch['input_ids'].to(self.device)
                attention_mask = batch['attention_mask'].to(self.device)
                labels = batch['labels'].to(self.device)
                outputs = self.model(input_ids, attention_mask=attention_mask, labels=labels, output_hidden_states=True)
                y_pred.append(outputs[1].detach().to('cpu').argmax(dim=1).numpy())
                y_true.append(labels.detach().to('cpu').numpy())
                if i % (len(self.val_loader) // 10) == 0:
                    print(f"{i}/{len(self.val_loader)}")
        y_pred = np.concatenate(y_pred, axis=0)
        y_true = np.concatenate(y_true, axis=0)
        acc = accuracy_score(y_true, y_pred)
        f1 = f1_score(y_true, y_pred)
        return acc, f1

    def calc_embeddings(self, topic_emb=False):
        '''
        Calculate the embeddings of all documents
        :param topic_emb: boolean. If False, then the document embedding is the original BERT embedding ([CLS] embedding)
            If True, the document embedding is the document-contextualized topic embedding, with more focus on the topic keywords.
        :return: the embeddings of all documents
        '''

        os.makedirs('embeddings', exist_ok=True)
        if not topic_emb:
            embedding_path = f'embeddings/embeddings_unfinetuned={self.args.unfinetuned}.pkl'
        else:
            embedding_path = f'embeddings/topic_embeddings_unfinetuned={self.args.unfinetuned}.pkl'
        if self.args.shuffle:
            embedding_path = embedding_path[:-4] + '_shuffle.pkl'
        if not os.path.exists(embedding_path):
            if not self.args.unfinetuned:
                self.model.load_state_dict(torch.load(self.model_path, map_location=self.device))
            embeddings = []
            self.model.eval()
            print('Calculating embedding....')
            with torch.no_grad():
                for i, batch in enumerate(self.loader):
                    input_ids = batch['input_ids'].to(self.device)
                    attention_mask = batch['attention_mask'].to(self.device)
                    outputs = self.model(input_ids, attention_mask=attention_mask, output_hidden_states=True)
                    if not topic_emb:
                        embeddings_ = outputs[1][-1][:, 0].detach().to('cpu').numpy()
                    else:
                        topic_masks = batch['topic_masks'].to(self.device).reshape(input_ids.shape[0],
                                                                                   input_ids.shape[1], -1)
                        embeddings_ = (topic_masks * outputs[1][-1]).sum(dim=1).detach().to('cpu').numpy()
                    embeddings.append(embeddings_)
                    if i % 50 == 0:
                        print(f"{i}/{len(self.loader)}")
            print('Done')
            embeddings = np.concatenate(embeddings, axis=0)
            pickle.dump(embeddings, open(embedding_path, 'wb'))
        else:
            embeddings = pickle.load(open(embedding_path, 'rb'))

        return embeddings

    def plot_embeddings(self, topic_emb=False, dim_reduction='pca'):
        '''
        Plot the document embeddings.
        :param topic_emb: see "calc_embeddings()"
        :param dim_reduction: which dimension reduction method to use. PCA or TSNE or UMAP
        :return:
        '''
        os.makedirs('embeddings', exist_ok=True)
        print('Plotting....')
        print('Reducing dimension....')
        if not topic_emb:
            embedding_path = f'embeddings/embeddings_{dim_reduction}_unfinetuned={self.args.unfinetuned}.pkl'
        else:
            embedding_path = f'embeddings/topic_embeddings_{dim_reduction}_unfinetuned={self.args.unfinetuned}.pkl'
        if self.args.shuffle:
            embedding_path = embedding_path[:-4] + '_shuffle.pkl'
        if not os.path.exists(embedding_path):
            embeddings = self.calc_embeddings(topic_emb)
            if dim_reduction == 'pca':
                from sklearn.decomposition import PCA
                embeddings2 = PCA(n_components=2).fit_transform(embeddings)
            elif dim_reduction == 'tsne':
                from sklearn.manifold import TSNE
                embeddings2 = TSNE(n_components=2).fit_transform(embeddings)
            else:
                from umap import UMAP
                embeddings2 = UMAP(n_neighbors=15, n_components=2, min_dist=0, metric='cosine').fit_transform(embeddings)
            pickle.dump(embeddings2, open(embedding_path, 'wb'))
        else:
            embeddings2 = pickle.load(open(embedding_path, 'rb'))
        print('Done')
        data = pd.DataFrame(embeddings2, columns=['x', 'y'])
        data['labels'] = self.labels
        df_doc_topic = pd.read_csv(os.path.join(self.args.data_path, 'df_doc_topic.csv'))
        df_doc_topic = df_doc_topic.sort_values(by=['prob'], ascending=False).drop_duplicates(subset='idx_doc',
                                                                                              keep='first')

        data['cluster_labels'] = -1
        data['cluster_labels'][df_doc_topic['idx_doc'].tolist()] = df_doc_topic['idx_topic'].tolist()
        # only plot the documents in the 10 labeled topics
        data = data[data['cluster_labels'].isin([1, 2, 8, 9, 10, 11, 12, 27, 30, 33])]

        import matplotlib.pyplot as plt
        clustered = data[data['cluster_labels'] != -1]
        clustered1 = clustered[clustered['labels'] == 0][:200]
        clustered2 = clustered[clustered['labels'] == 1][:200]

        from matplotlib.backends.backend_pdf import PdfPages
        os.makedirs('fig', exist_ok=True)
        if not topic_emb:
            fig_name = f'fig/embeddings_{dim_reduction}_unfinetuned={self.args.unfinetuned}.pdf'
            print(fig_name)
        else:
            fig_name = f'fig/topic_embeddings_{dim_reduction}_unfinetuned={self.args.unfinetuned}.pdf'
            print(fig_name)

        with PdfPages(fig_name) as pdf:
            _, _ = plt.subplots(figsize=(5, 5))
            plt.scatter(clustered1.x, clustered1.y, c=clustered1['cluster_labels'], marker='o', s=30, cmap='hsv_r', alpha=0.2,
                        label='liberal')
            plt.scatter(clustered2.x, clustered2.y, c=clustered2['cluster_labels'], marker='x', s=30, cmap='hsv_r', alpha=0.5,
                        label='conservative')

            plt.xlabel('dim_1', fontsize=12)
            plt.ylabel('dim_2', fontsize=12)
            plt.legend(fontsize=12)
            pdf.savefig()

    def get_polarization(self, args):
        '''
        calculate the polarization score for each topic and save the ranking
        '''

        def select_docs(df_doc_topic, topic_idx, source, month, max_docs=10, min_docs=2):
            '''
            output the top-n documents from each source for each topic
            '''
            if not isinstance(source, list):
                source = [source]
            if not isinstance(month, list):
                month = [month]

            df = df_doc_topic[(df_doc_topic['idx_topic'] == topic_idx) &
                              (df_doc_topic['month'].isin(month)) &
                              (df_doc_topic['source'].isin(source))].sort_values(by=['prob'],
                                                                                 ascending=False).head(max_docs)
            if df.shape[0] >= min_docs:
                return df['idx_doc'].tolist(), df['prob'].tolist()
            return [], []

        def calc_corpus_embedding(text_embeddings, text_probs):
            # calculate corpus-contextualized document embeddings
            # text_probs: the probabilities of a doc associated with the topic
            if len(text_embeddings) != 0:
                text_probs = np.array(text_probs)
                text_probs /= text_probs.mean()
                text_probs = text_probs.reshape(-1, 1)
                return (text_probs * text_embeddings).mean(axis=0)
            else:
                return np.zeros(768)

        topics = pickle.load(open(os.path.join(self.args.data_path, 'topics.pkl'), 'rb'))
        topic_stems = [[each[0] for each in each1[1]] for each1 in topics]

        if args.polarization in ['emb', 'emb_pairwise']:
            doc_embeddings = self.calc_embeddings(True)
        elif args.polarization == 'emb_doc':
            doc_embeddings = self.calc_embeddings()
        else:
            doc_embeddings = None

        df_doc_topic = pd.read_csv(os.path.join(self.args.data_path, 'df_doc_topic.csv'))

        ### the annotations of the document leanings for documents in the 10 labels topics
        doc_idx2label = pickle.load(open(os.path.join(self.args.data_path, 'doc_idx2label.pkl'), 'rb'))

        topic_ranks = pickle.load(open(os.path.join(self.args.data_path, 'topic_ranks.pkl'), 'rb'))
        # topic_idxes = [each[0] for each in topic_ranks]
        topic_idxes = [1, 2, 8, 9, 10, 11, 12, 27, 30, 33]
        months = sorted(df_doc_topic['month'].unique().tolist())

        if args.polarization in ['emb', 'emb_pairwise', 'emb_doc']:
            corpus, id2word = None, None
        else:
            corpus, id2word = pickle.load(open(os.path.join(self.args.data_path, 'corpus_lo.pkl'), 'rb'))

        data = []
        from sklearn.metrics.pairwise import cosine_similarity
        for topic_idx in topic_idxes:
            print(f"{'*' * 10}Topic: {topic_idx}{'*' * 10}")
            row = [','.join(topic_stems[topic_idx]), f'topic_{topic_idx}']

            months_ = [months]
            for month in months_:
                idxes_docs1, text_probs1 = select_docs(df_doc_topic, topic_idx, self.args.source1, month,
                                                       self.args.max_docs, self.args.min_docs)
                idxes_docs2, text_probs2 = select_docs(df_doc_topic, topic_idx, self.args.source2, month,
                                                       self.args.max_docs, self.args.min_docs)
                min_len = min(len(idxes_docs1), len(idxes_docs2))
                print(f"month:{month}/{max(months)}, n_docs:{min_len}")
                idxes_docs1_ = idxes_docs1[:min_len]
                idxes_docs2_ = idxes_docs2[:min_len]
                text_probs1 = np.array(text_probs1[:min_len])
                text_probs2 = np.array(text_probs2[:min_len])
                text_probs1 /= text_probs1.mean()
                text_probs2 /= text_probs2.mean()

                if self.args.polarization in ['emb', 'emb_pairwise', 'emb_doc']:

                    if args.polarization in ['emb', 'emb_doc']:
                        emb1 = calc_corpus_embedding(doc_embeddings[idxes_docs1_], text_probs1)
                        emb2 = calc_corpus_embedding(doc_embeddings[idxes_docs2_], text_probs2)
                        cos_sim = cosine_similarity([emb1], [emb2])[0][0]
                    else:
                        embs1_ = doc_embeddings[idxes_docs1_]
                        embs2_ = doc_embeddings[idxes_docs2_]
                        if embs1_.sum() != 0 and embs2_.sum() != 0:
                            pairwise_cossim = cosine_similarity(embs1_, embs2_)
                            weight_mat = np.matmul(np.array(text_probs1).reshape(-1, 1),
                                                   np.array(text_probs2).reshape(1, -1))
                            # weight_mat = np.ones((min_len, min_len))
                            weight_mat = weight_mat / weight_mat.mean()
                            cos_sim = (pairwise_cossim * weight_mat).mean()
                        else:
                            cos_sim = float('nan')
                    pola_score = 0.5 * (-cos_sim + 1)

                elif self.args.polarization == 'lo':
                    corpus1 = pd.Series(corpus)[idxes_docs1]
                    corpus2 = pd.Series(corpus)[idxes_docs2]
                    pola_score, pol_score_random, n_articles = get_leaveout_score(corpus1, corpus2, id2word,
                                                                                  min_docs=args.min_docs,
                                                                                  max_docs=args.max_docs)
                    # pola_score = 1 - 2 * pola_score
                else:  # ground true
                    annotations1 = [doc_idx2label[each] for each in idxes_docs1]
                    annotations2 = [doc_idx2label[each] for each in idxes_docs2]
                    label2indexes1 = {0: [], 1: [], -1: []}
                    label2indexes2 = {0: [], 1: [], -1: []}
                    for i, anno in enumerate(annotations1):
                        label2indexes1[anno].append(i)
                    for i, anno in enumerate(annotations2):
                        label2indexes2[anno].append(i)

                    x = 0
                    for i in range(len(label2indexes1[1])):
                        index = label2indexes1[1][i]
                        prob = text_probs1[index]
                        x += prob
                    y = 0
                    for i in range(len(label2indexes1[0])):
                        index = label2indexes1[0][i]
                        prob = text_probs1[index]
                        y += prob
                    score1 = (x-y)/len(annotations1)

                    x = 0
                    for i in range(len(label2indexes2[1])):
                        index = label2indexes2[1][i]
                        prob = text_probs2[index]
                        x += prob
                    y = 0
                    for i in range(len(label2indexes2[0])):
                        index = label2indexes2[0][i]
                        prob = text_probs2[index]
                        y += prob
                    score2 = (x - y) / len(annotations2)

                    pola_score = np.abs(score1 - score2) / 2

                # pola_score = float('nan') if pola_score == 0 else pola_score
                row.append(pola_score)

            data.append([row[1], row[2], row[0]])

        os.makedirs('results', exist_ok=True)
        file_name = f"{args.source1}_{args.source2}{'_unfinetuned' if args.unfinetuned else ''}_{args.polarization}" \
                    f"_{args.max_docs}_{args.min_docs}.csv"
        if self.args.shuffle:
            file_name = file_name[:-5] + '_shuffle.csv'
        data.sort(key=lambda x: x[1], reverse=True)
        df = pd.DataFrame(data, columns=['topic_idx', 'pola'] + ['topic_words'])
        return df

def get_news_token_counts(corpus, idx2word):
    row_idx = []
    col_idx = []
    data = []
    for i, doc in enumerate(corpus):
        for j, count in doc:
            row_idx.append(i)
            col_idx.append(j)
            data.append(count)
    return sp.csr_matrix((data, (row_idx, col_idx)), shape=(len(corpus), len(idx2word)))



def get_party_q(party_counts, exclude_user_id = None):
    user_sum = party_counts.sum(axis=0)
    if exclude_user_id:
        user_sum -= party_counts[exclude_user_id, :]
    total_sum = user_sum.sum()
    return user_sum / total_sum


def get_rho(dem_q, rep_q):
    return (rep_q / (dem_q + rep_q)).transpose()


def get_token_user_counts(party_counts):
    no_tokens = party_counts.shape[1]
    nonzero = sp.find(party_counts)[:2]
    user_t_counts = Counter(nonzero[1])  # number of users using each term
    party_t = np.ones(no_tokens)  # add one smoothing
    for k, v in user_t_counts.items():
        party_t[k] += v
    return party_t


def mutual_information(dem_t, rep_t, dem_not_t, rep_not_t, dem_no, rep_no):
    no_users = dem_no + rep_no
    all_t = dem_t + rep_t
    all_not_t = no_users - all_t + 4
    mi_dem_t = dem_t * np.log2(no_users * (dem_t / (all_t * dem_no)))
    mi_dem_not_t = dem_not_t * np.log2(no_users * (dem_not_t / (all_not_t * dem_no)))
    mi_rep_t = rep_t * np.log2(no_users * (rep_t / (all_t * rep_no)))
    mi_rep_not_t = rep_not_t * np.log2(no_users * (rep_not_t / (all_not_t * rep_no)))
    return (1 / no_users * (mi_dem_t + mi_dem_not_t + mi_rep_t + mi_rep_not_t)).transpose()[:, np.newaxis]


def chi_square(dem_t, rep_t, dem_not_t, rep_not_t, dem_no, rep_no):
    no_users = dem_no + rep_no
    all_t = dem_t + rep_t
    all_not_t = no_users - all_t + 4
    chi_enum = no_users * (dem_t * rep_not_t - dem_not_t * rep_t) ** 2
    chi_denom = all_t * all_not_t * (dem_t + dem_not_t) * (rep_t + rep_not_t)
    return (chi_enum / chi_denom).transpose()[:, np.newaxis]


def calculate_polarization(dem_counts, rep_counts, measure="posterior", leaveout=True):
    dem_user_total = dem_counts.sum(axis=1)
    rep_user_total = rep_counts.sum(axis=1)

    dem_user_distr = (sp.diags(1 / dem_user_total.A.ravel())).dot(dem_counts)  # get row-wise distributions
    rep_user_distr = (sp.diags(1 / rep_user_total.A.ravel())).dot(rep_counts)
    dem_no = dem_counts.shape[0]
    rep_no = rep_counts.shape[0]
    assert (set(dem_user_total.nonzero()[0]) == set(range(dem_no)))  # make sure there are no zero rows
    assert (set(rep_user_total.nonzero()[0]) == set(range(rep_no)))  # make sure there are no zero rows
    if measure not in ('posterior', 'mutual_information', 'chi_square'):
        print('invalid method')
        return
    dem_q = get_party_q(dem_counts)
    rep_q = get_party_q(rep_counts)
    dem_t = get_token_user_counts(dem_counts)
    rep_t = get_token_user_counts(rep_counts)
    dem_not_t = dem_no - dem_t + 2  # because of add one smoothing
    rep_not_t = rep_no - rep_t + 2  # because of add one smoothing
    func = mutual_information if measure == 'mutual_information' else chi_square

    # apply measure without leave-out
    if not leaveout:
        if measure == 'posterior':
            token_scores_rep = get_rho(dem_q, rep_q)
            token_scores_dem = 1. - token_scores_rep
        else:
            token_scores_dem = func(dem_t, rep_t, dem_not_t, rep_not_t, dem_no, rep_no)
            token_scores_rep = token_scores_dem
        dem_val = 1 / dem_no * dem_user_distr.dot(token_scores_dem).sum()
        rep_val = 1 / rep_no * rep_user_distr.dot(token_scores_rep).sum()
        return 1/2 * (dem_val + rep_val)

    # apply measures via leave-out
    dem_addup = 0
    rep_addup = 0
    dem_leaveout_no = dem_no - 1
    rep_leaveout_no = rep_no - 1
    for i in range(dem_no):
        if measure == 'posterior':
            dem_leaveout_q = get_party_q(dem_counts, i)
            token_scores_dem = 1. - get_rho(dem_leaveout_q, rep_q)
        else:
            dem_leaveout_t = dem_t.copy()
            excl_user_terms = sp.find(dem_counts[i, :])[1]
            for term_idx in excl_user_terms:
                dem_leaveout_t[term_idx] -= 1
            dem_leaveout_not_t = dem_leaveout_no - dem_leaveout_t + 2
            token_scores_dem = func(dem_leaveout_t, rep_t, dem_leaveout_not_t, rep_not_t, dem_leaveout_no, rep_no)
        dem_addup += dem_user_distr[i, :].dot(token_scores_dem)[0, 0]
    for i in range(rep_no):
        if measure == 'posterior':
            rep_leaveout_q = get_party_q(rep_counts, i)
            token_scores_rep = get_rho(dem_q, rep_leaveout_q)
        else:
            rep_leaveout_t = rep_t.copy()
            excl_user_terms = sp.find(rep_counts[i, :])[1]
            for term_idx in excl_user_terms:
                rep_leaveout_t[term_idx] -= 1
            rep_leaveout_not_t = rep_leaveout_no - rep_leaveout_t + 2
            token_scores_rep = func(dem_t, rep_leaveout_t, dem_not_t, rep_leaveout_not_t, dem_no, rep_leaveout_no)
        rep_addup += rep_user_distr[i, :].dot(token_scores_rep)[0, 0]
    rep_val = 1 / rep_no * rep_addup
    dem_val = 1 / dem_no * dem_addup
    return 1/2 * (dem_val + rep_val)


def get_leaveout_score(corpus1, corpus2, id2word, token_partisanship_measure='posterior',
                       leaveout=True, default_score=0.5, min_docs=10, max_docs=999):
    """
    Measure polarization.
    :param event: name of the event
    :param data: dataframe with 'text' and 'user_id'
    :param token_partisanship_measure: type of measure for calculating token partisanship based on user-token counts
    :param leaveout: whether to use leave-out estimation
    :param between_topic: whether the estimate is between topics or tokens
    :param default_score: default token partisanship score
    :return:
    """
    import gc

    dem_counts = get_news_token_counts(corpus1, id2word)
    rep_counts = get_news_token_counts(corpus2, id2word)

    dem_user_len = dem_counts.shape[0]
    rep_user_len = rep_counts.shape[0]

    # return these values when there is not enough data to make predictions on
    if dem_user_len < min_docs or rep_user_len < min_docs:
        return default_score, default_score, dem_user_len + rep_user_len

    if max_docs < dem_user_len:
        dem_counts = dem_counts[:dem_user_len]
    if max_docs < rep_user_len:
        rep_counts = rep_counts[:rep_user_len]

    import random
    RNG = random.Random()  # make everything reproducible
    RNG.seed(42)
    # make the prior neutral (i.e. make sure there are the same number of Rep and Dem users)
    dem_user_len = dem_counts.shape[0]
    rep_user_len = rep_counts.shape[0]
    if dem_user_len > rep_user_len:
        dem_subset = np.array(RNG.sample(range(dem_user_len), rep_user_len))
        dem_counts = dem_counts[dem_subset, :]
        dem_user_len = dem_counts.shape[0]
    elif rep_user_len > dem_user_len:
        rep_subset = np.array(RNG.sample(range(rep_user_len), dem_user_len))
        rep_counts = rep_counts[rep_subset, :]
        rep_user_len = rep_counts.shape[0]
    assert (dem_user_len == rep_user_len)

    all_counts = sp.vstack([dem_counts, rep_counts])

    wordcounts = all_counts.nonzero()[1]

    # filter words used by fewer than 2 people
    all_counts = all_counts[:, np.array([(np.count_nonzero(wordcounts == i) > 1) for i in range(all_counts.shape[1])])]

    dem_counts = all_counts[:dem_user_len, :]
    rep_counts = all_counts[dem_user_len:, :]
    del wordcounts
    del all_counts
    gc.collect()

    dem_nonzero = set(dem_counts.nonzero()[0])
    rep_nonzero = set(rep_counts.nonzero()[0])
    # filter users who did not use words from vocab
    dem_counts = dem_counts[np.array([(i in dem_nonzero) for i in range(dem_counts.shape[0])]), :]
    rep_counts = rep_counts[np.array([(i in rep_nonzero) for i in range(rep_counts.shape[0])]), :]
    del dem_nonzero
    del rep_nonzero
    gc.collect()

    actual_val = calculate_polarization(dem_counts, rep_counts, token_partisanship_measure, leaveout)

    all_counts = sp.vstack([dem_counts, rep_counts])
    del dem_counts
    del rep_counts
    gc.collect()

    index = np.arange(all_counts.shape[0])
    RNG.shuffle(index)
    all_counts = all_counts[index, :]

    random_val = calculate_polarization(all_counts[:dem_user_len, :], all_counts[dem_user_len:, :],
                                        token_partisanship_measure, leaveout)
    print(actual_val, random_val, dem_user_len + rep_user_len)
    sys.stdout.flush()
    del all_counts
    gc.collect()

    return actual_val, random_val, dem_user_len + rep_user_len



def get_leaveout_emb_score(dem_counts, rep_counts, token_partisanship_measure='posterior',
                       leaveout=True, default_score=0.5, min_docs=10, max_docs=999):
    """
    Measure polarization.
    :param event: name of the event
    :param data: dataframe with 'text' and 'user_id'
    :param token_partisanship_measure: type of measure for calculating token partisanship based on user-token counts
    :param leaveout: whether to use leave-out estimation
    :param between_topic: whether the estimate is between topics or tokens
    :param default_score: default token partisanship score
    :return:
    """
    if dem_counts.sum() == 0 or rep_counts.sum() == 0:
        return 0.5, 0, 0
    from sklearn.metrics.pairwise import cosine_similarity
    sim_mat = cosine_similarity(dem_counts, rep_counts)
    # import ipdb; ipdb.set_trace()
    # print(sim_mat)
    return sim_mat.mean(), 0, 0



def pacte(docs):
    data = docs

    data_path = './data/pacte'
    # https://github.com/zihaohe123/pacte-polarized-topics-detection
    texts_processed_lda, corpus_lda, id2word_lda = preprocessing_lda(data)
    pickle.dump(texts_processed_lda, open(os.path.join(data_path, 'texts_processed_lda.pkl'), 'wb'))

    text_processed_bert = preprocessing_bert(data)
    pickle.dump(text_processed_bert, open(os.path.join(data_path, 'texts_processed_bert.pkl'), 'wb'))

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--data_path', type=str)

    # training BERT model
    parser.add_argument('--lr', type=float, default=1e-5)
    parser.add_argument('--batch_size', type=int, default=24)
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--unfinetuned', type=int, choices=(0, 1), default=0,
                        help='whether to finetune the language model or not')
    parser.add_argument('--gpu', type=str, default='', help='which gpus to use, starting from 0')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--init_train', type=int, choices=(0,1), default=1)

    parser.add_argument('--shuffle', type=int, choices=(0, 1), default=0,
                        help='whether to shuffle the partisanship labels or not')

    # plotting
    parser.add_argument('--plotting', type=int, choices=(0, 1), default=0, help='whether to plot the embeddings or not')
    parser.add_argument('--dim_reduction', type=str, choices=('pca', 'tsne', 'umap'), default='tsne', help='which ')

    # calculating polarization
    parser.add_argument('--polarization', type=str,
                        choices=('emb', # pacte
                                 'lo',  # leaveout estimator
                                 'emb_pairwise',   # a baseline, never mind
                                 'gt',  # ground truth from annotations
                                 'emb_doc'  # pacte, but the document embedding is the holistic CLS embedding
                                ),
                        default='emb',
                        help='the method to use to calculate the polarization scores')
    parser.add_argument('--source1', nargs='+', default=['cnn', 'huff', 'nyt'], help='the left sources')
    parser.add_argument('--source2', nargs='+', default=['fox', 'breit', 'nyp'], help='the right sources')
    parser.add_argument('--n_topics', type=int, default=10)
    parser.add_argument('--max_docs', type=int, default=10, help='max # of documents for a topic from each source')
    parser.add_argument('--min_docs', type=int, default=10, help='min # of documents for a topic from each source')

    args = parser.parse_args()

    args.data_path = data_path

    if args.polarization in ['lo', 'gt']:
        args.unfinetuned = 0
        args.init_train = 0
        # args.plotting = 0

    args.unfinetuned = {0: False, 1: True}[args.unfinetuned]
    args.init_train = {0: False, 1: True}[args.init_train]
    args.plotting = {0: False, 1: True}[args.plotting]
    args.shuffle = {0: False, 1: True}[args.shuffle]

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    np.random.seed(args.seed)
    engine = Engine(args)
    if args.init_train:
        engine.train()
    df = engine.get_polarization(args)
    if args.plotting:
        engine.plot_embeddings(dim_reduction='pca')
        engine.plot_embeddings(dim_reduction='tsne')
        engine.plot_embeddings(topic_emb=True, dim_reduction='pca')
        engine.plot_embeddings(topic_emb=True, dim_reduction='tsne')

    return df
