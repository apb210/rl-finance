import torch
import torch.nn as nn
from torch.nn import LSTM, LSTMCell, Linear
from torch.distributions.one_hot_categorical import OneHotCategorical
from torch.distributions import Normal


class Encoder(nn.Module):

    def __init__(self, input_size, batch_size=1, hidden_size=128, num_layers=2, dropout=0.85):
        """ Construct a multilayer LSTM that computes the encoding vector"""

        super(Encoder, self).__init__()

        self.num_layers = num_layers
        self.hidden_size = hidden_size
        self.batch_size = batch_size

        self.LSTM = LSTM(input_size=input_size, hidden_size=hidden_size, num_layers=num_layers, batch_first=True,
                         dropout=dropout)          # encoding size is the same as the hidden size

    def forward(self, input, h_0=None, c_0=None):
        """ input should have size (batch_size, seq_len, input_size)"""

        # Forward propagation to calculate the encoding vector
        # We only care about the final unit output, i.e., the hidden state of the final unit
        if (h_0 is not None) and (c_0 is not None):
            _, (h_n, _) = self.LSTM(input, (h_0, c_0))
        else:
            _, (h_n, _) = self.LSTM(input)
        # h_n should have shape (num_layers, batch_size, hidden_size). We only want the content from the last layer
        encoding = h_n[-1, :, :]
        # squeeze the encoding vector so that it has shape (batch_size, hidden_size)
        encoding = encoding.squeeze(1)

        return encoding


class PolicyNet(nn.Module):

    def __init__(self, state_size, num_actions, act_lim=1, batch_size=1, hidden_size=128, num_layers=2, dropout=0.85):
        """ Construct a multilayer LSTM that computes the action given the state

            The agent will first decide which dimension to act on and then decide the numerical value of the aciton on that dimension

            - shape of input state is given by state_size
            - dimensions of the orthogonal action space is given by num_actions, whereas act_lim gives the numerical bound for action values
                Note: the last action dimension is assumed to be discrete, meaning the agent "does nothing".
            - hidden_size should match that of the encoding network (i.e. the size of the encoding layer)

        """
        super(PolicyNet, self).__init__()

        self.state_size = state_size
        self.num_actions = num_actions
        self.act_lim = act_lim
        self.batch_size = batch_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.dropout = dropout

        # Create multilayer LSTM cells
        self.cell_list = nn.ModuleList()
        self.cell_list.append(LSTMCell(input_size=state_size, hidden_size=hidden_size))
        for i in range(1, num_layers):
            self.cell_list.append(LSTMCell(input_size=hidden_size, hidden_size=hidden_size))

        # Linear layer that decides the dimension the agent wants to act on.
        #   Return the logits to be used to construct a Categorical distribution
        self.FC_decision = Linear(hidden_size, num_actions)
        # Linear layer that computes the mean value of the agent's action on each dimension
        self.FC_values_mean = Linear(hidden_size, num_actions)
        # Linear layer that computes the log standard deviation of the agent's action on each dimension
        self.FC_values_logstd = Linear(hidden_size, num_actions)

        # Variables to store lists of hidden states and cell states at the end of each time step so as to be used as
        #   the input values to the next time step
        # Reset to None at the start of each episode
        self.h_list = None
        self.c_list = None

    def forward(self, state, encoding=None, device='cpu'):
        """
            - At the first time step, pass in the encoding vector from Encoder with shape (batch_size, hidden_size)
                using the optional argument encoding= . h_list and c_list will be reset to 0s
            - At the following time steps, DO NOT pass in any value to the optional argument encoding=
        """

        # TODO: Test the dimensions of this multilayer LSTM policy net

        # If encoding is not None, reset lists of hidden states and cell states
        if encoding is not None:
            self.h_list = [torch.zeros((self.batch_size, self.hidden_size), device=device) * self.num_layers]
            self.c_list = [torch.zeros((self.batch_size, self.hidden_size), device=device) * self.num_layers]
            self.h_list[0] = encoding

        # Forward propagation
        h1_list = []
        c1_list = []
        # First layer
        h_1, c_1 = self.cell_list[0](state, (self.h_list[0], self.c_list[0]))
        h1_list.append(h_1)
        c1_list.append(c_1)
        # Following layers
        for i in range(1, self.num_layers):
            h_1, c_1 = self.cell_list[i](h_1, (self.h_list[0], self.c_list[0]))
            h1_list.append(h_1)
            c1_list.append(c_1)
        # Store hidden states list and cell state list
        self.h_list = h1_list
        self.c_list = c1_list

        decision_logit = self.FC_decision(h_1)
        values_mean = self.FC_values_mean(h_1)
        values_logstd = self.FC_values_logstd(h_1)

        # Take the exponentials of log standard deviation
        values_std = torch.exp(values_logstd)

        # Create a categorical (multinomial) distribution from which we can sample a decision on the action dimension
        m_decision = OneHotCategorical(logits=decision_logit)

        # Sample a decision and calculate its log probability. decision of shape (num_actions,)
        decision = m_decision.sample()
        decision_log_prob = m_decision.log_prob(decision)

        # Create a list of Normal distributions for sampling actions in each dimension
        # Note: the last action is assumed to be discrete, meaning "doing nothing", so it has a conditional probability
        #       of 1.
        m_values = []
        action_values = None
        actions_log_prob = None
        # All actions except the last one are assumed to have normal distribution
        for i in range(self.num_actions - 1):
            m_values.append(Normal(values_mean[:, i], values_std[:, i]))
            if action_values is None:
                action_values = m_values[-1].sample().unsqueeze(1)                    # Unsqueeze to spare the batch dimension
                actions_log_prob = m_values[-1].log_prob(action_values[:, -1]).unsqueeze(1)
            else:
                action_values = torch.cat([action_values, m_values[-1].sample().unsqueeze(1)], dim=1)
                actions_log_prob = torch.cat([actions_log_prob, m_values[-1].log_prob(action_values[:, -1]).unsqueeze(1)], dim=1)

        # TODO: Append the last action. The last action has value 0.0 and log probability 0.0.
        action_values = torch.cat([action_values, torch.zeros((self.batch_size, 1), device=device)], dim=1)
        actions_log_prob = torch.cat([actions_log_prob, torch.zeros((self.batch_size, 1), device=device)], dim=1)

        # Filter the final action value in the intended action dimension
        final_action_values = (action_values * decision).sum(dim=1)
        final_action_log_prob = (actions_log_prob * decision).sum(dim=1)



        # Scale the action value by act_lim
        final_action_values = final_action_values * self.act_lim

        # Calculate the final log probability
        #   Pr(action value in the ith dimension) = Pr(action value given the agent chooses the ith dimension)
        #                                           * Pr(the agent chooses the ith dimension
        log_prob = decision_log_prob + final_action_log_prob


        return decision, final_action_values, log_prob


def optimize_model(policy_net, batch_log_prob, batch_rewards, optimizer, GAMMA=0.999, device='cuda'):
    """ Optimize the model for one step"""

    # Obtain batch size
    batch_size = len(batch_log_prob)

    # Calculate weight
    # Simple Policy Gradient: Use trajectory Reward To Go
    batch_weight = []
    for rewards in batch_rewards:
        n = rewards.shape[0]
        rtg = torch.zeros(n, device=device)
        for i in reversed(range(n)):
            rtg[i] = rewards[i] + (GAMMA * rtg[i+1] if i + 1 < n else 0)
        batch_weight.append(rtg)

    # Calculate grad-prob-log
    loss = None
    for i in range(batch_size):
        if loss is None:
            loss = - torch.sum(batch_log_prob[i] * batch_weight[i])
        else:
            loss += - torch.sum(batch_log_prob[i] * batch_weight[i])

    loss = loss / torch.tensor(batch_size, device=device)
    # Gradient Ascent
    optimizer.zero_grad()
    loss.backward()

    for param in policy_net.parameters():
        param.grad.data.clamp_(-1, 1)
    optimizer.step()
