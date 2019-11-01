import torch
import torch.nn as nn
import torch.nn.functional as F

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


class Encoder2(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim, n_layers, kernel_size, dropout, device):
        super().__init__()

        assert kernel_size % 2 == 1, "Kernel size must be odd!"

        self.input_dim = input_dim
        self.emb_dim = emb_dim
        self.hid_dim = hid_dim
        self.kernel_size = kernel_size
        self.dropout = dropout
        self.device = device

        self.scale = torch.sqrt(torch.FloatTensor([0.5])).to(device)

        self.tok_embedding = nn.Embedding(input_dim, emb_dim)
        self.pos_embedding = nn.Embedding(100, emb_dim)

        self.emb2hid = nn.Linear(emb_dim, hid_dim)
        self.hid2emb = nn.Linear(hid_dim, emb_dim)

        self.convs = nn.ModuleList([nn.Conv1d(in_channels=hid_dim,
                                              out_channels=2 * hid_dim,
                                              kernel_size=kernel_size,
                                              padding=(kernel_size - 1) // 2)
                                    for _ in range(n_layers)])

        self.dropout = nn.Dropout(dropout)

    def forward(self, src):
        # src = [batch size, src sent len]

        # create position tensor
        pos = torch.arange(0, src.shape[1]).unsqueeze(0).repeat(src.shape[0], 1).to(self.device)

        # pos = [batch size, src sent len]

        # embed tokens and positions
        tok_embedded = self.tok_embedding(src)
        pos_embedded = self.pos_embedding(pos)

        # tok_embedded = pos_embedded = [batch size, src sent len, emb dim]

        # combine embeddings by elementwise summing
        embedded = self.dropout(tok_embedded + pos_embedded)

        # embedded = [batch size, src sent len, emb dim]

        # pass embedded through linear layer to go through emb dim -> hid dim
        conv_input = self.emb2hid(embedded)

        # conv_input = [batch size, src sent len, hid dim]

        # permute for convolutional layer
        conv_input = conv_input.permute(0, 2, 1)

        # conv_input = [batch size, hid dim, src sent len]

        for i, conv in enumerate(self.convs):
            # pass through convolutional layer
            conved = conv(self.dropout(conv_input))

            # conved = [batch size, 2*hid dim, src sent len]

            # pass through GLU activation function
            conved = F.glu(conved, dim=1)

            # conved = [batch size, hid dim, src sent len]

            # apply residual connection
            conved = (conved + conv_input) * self.scale
            # conved = [batch size, hid dim, src sent len]

            # set conv_input to conved for next loop iteration
            conv_input = conved

            # permute and convert back to emb dim
        conved = self.hid2emb(conved.permute(0, 2, 1))

        # conved = [batch size, src sent len, emb dim]

        # elementwise sum output (conved) and input (embedded) to be used for attention
        combined = (conved + embedded) * self.scale

        # combined = [batch size, src sent len, emb dim]

        return conved, combined


class Decoder2(nn.Module):
    def __init__(self, output_dim, emb_dim, hid_dim, n_layers, kernel_size, dropout, pad_idx, device):
        super().__init__()

        self.output_dim = output_dim
        self.emb_dim = emb_dim
        self.hid_dim = hid_dim
        self.kernel_size = kernel_size
        self.dropout = dropout
        self.pad_idx = pad_idx
        self.device = device

        self.scale = torch.sqrt(torch.FloatTensor([0.5])).to(device)

        self.tok_embedding = nn.Embedding(output_dim, emb_dim)
        self.pos_embedding = nn.Embedding(100, emb_dim)

        self.emb2hid = nn.Linear(emb_dim, hid_dim)
        self.hid2emb = nn.Linear(hid_dim, emb_dim)

        self.attn_hid2emb = nn.Linear(hid_dim, emb_dim)
        self.attn_emb2hid = nn.Linear(emb_dim, hid_dim)

        self.out = nn.Linear(emb_dim, output_dim)

        self.convs = nn.ModuleList([nn.Conv1d(hid_dim, 2 * hid_dim, kernel_size)
                                    for _ in range(n_layers)])

        self.dropout = nn.Dropout(dropout)

    def calculate_attention(self, embedded, conved, encoder_conved, encoder_combined):
        # embedded = [batch size, trg sent len, emb dim]
        # conved = [batch size, hid dim, trg sent len]
        # encoder_conved = encoder_combined = [batch size, src sent len, emb dim]

        # permute and convert back to emb dim
        conved_emb = self.attn_hid2emb(conved.permute(0, 2, 1))

        # conved_emb = [batch size, trg sent len, emb dim]

        combined = (conved_emb + embedded) * self.scale

        # combined = [batch size, trg sent len, emb dim]

        energy = torch.matmul(combined, encoder_conved.permute(0, 2, 1))

        # energy = [batch size, trg sent len, src sent len]

        attention = F.softmax(energy, dim=2)

        # attention = [batch size, trg sent len, src sent len]

        attended_encoding = torch.matmul(attention, encoder_combined)

        # attended_encoding = [batch size, trg sent len, emd dim]

        # convert from emb dim -> hid dim
        attended_encoding = self.attn_emb2hid(attended_encoding)

        # attended_encoding = [batch size, trg sent len, hid dim]

        # apply residual connection
        attended_combined = (conved + attended_encoding.permute(0, 2, 1)) * self.scale

        # attended_combined = [batch size, hid dim, trg sent len]

        return attention, attended_combined

    def forward(self, trg, encoder_conved, encoder_combined):
        # trg = [batch size, trg sent len]
        # encoder_conved = encoder_combined = [batch size, src sent len, emb dim]

        # create position tensor
        pos = torch.arange(0, trg.shape[1]).unsqueeze(0).repeat(trg.shape[0], 1).to(device)

        # pos = [batch size, trg sent len]

        # embed tokens and positions
        tok_embedded = self.tok_embedding(trg)
        pos_embedded = self.pos_embedding(pos)

        # tok_embedded = [batch size, trg sent len, emb dim]
        # pos_embedded = [batch size, trg sent len, emb dim]

        # combine embeddings by elementwise summing
        embedded = self.dropout(tok_embedded + pos_embedded)

        # embedded = [batch size, trg sent len, emb dim]

        # pass embedded through linear layer to go through emb dim -> hid dim
        conv_input = self.emb2hid(embedded)

        # conv_input = [batch size, trg sent len, hid dim]

        # permute for convolutional layer
        conv_input = conv_input.permute(0, 2, 1)

        # conv_input = [batch size, hid dim, trg sent len]
        for i, conv in enumerate(self.convs):
            # apply dropout
            conv_input = self.dropout(conv_input)

            # need to pad so decoder can't "cheat"
            padding = torch.zeros(conv_input.shape[0], conv_input.shape[1], self.kernel_size - 1).fill_(
                self.pad_idx).to(device)
            padded_conv_input = torch.cat((padding, conv_input), dim=2)

            # padded_conv_input = [batch size, hid dim, trg sent len + kernel size - 1]

            # pass through convolutional layer
            conved = conv(padded_conv_input)

            # conved = [batch size, 2*hid dim, trg sent len]

            # pass through GLU activation function
            conved = F.glu(conved, dim=1)

            # conved = [batch size, hid dim, trg sent len]

            # calculate attention
            attention, conved = self.calculate_attention(embedded, conved, encoder_conved, encoder_combined)

            # attention = [batch size, trg sent len, src sent len]

            # apply residual connection
            conved = (conved + conv_input) * self.scale

            # set conv_input to conved for next loop iteration
            conv_input = conved

        conved = self.hid2emb(conved.permute(0, 2, 1))

        # conved = [batch size, trg sent len, hid dim]

        output = self.out(self.dropout(conved))

        # output = [batch size, trg sent len, output dim]

        return output, attention


class Seq2Seq2(nn.Module):
    def __init__(self, encoder, decoder, device):
        super().__init__()

        self.encoder = encoder
        self.decoder = decoder
        self.device = device

    def forward(self, src, trg):
        # src = [batch size, src sent len]
        # trg = [batch size, trg sent len]

        # calculate z^u (encoder_conved) and e (encoder_combined)
        # encoder_conved is output from final encoder conv. block
        # encoder_combined is encoder_conved plus (elementwise) src embedding plus positional embeddings
        encoder_conved, encoder_combined = self.encoder(src)

        # encoder_conved = [batch size, src sent len, emb dim]
        # encoder_combined = [batch size, src sent len, emb dim]

        # calculate predictions of next words
        # output is a batch of predictions for each word in the trg sentence
        # attention a batch of attention scores across the src sentence for each word in the trg sentence
        output, attention = self.decoder(trg, encoder_conved, encoder_combined)

        # output = [batch size, trg sent len, output dim]
        # attention = [batch size, trg sent len, src sent len]

        return output, attention